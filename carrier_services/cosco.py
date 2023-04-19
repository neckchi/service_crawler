from crawler_modal.async_crawler import *
from schemas.service_loops import Services
from urllib.parse import urlparse
from carrier_services.helpers import order_counter
from logger_factory.logger import LoggerFactory
from schemas import settings
import concurrent.futures
import functools
import uuid
import json
import time
import os
import csv

logger = LoggerFactory.get_logger(__name__, log_level="INFO")


def cosco_mapping(crawler_result: list, writer: csv.DictWriter):
    # File Operation IO tasks - sort out the file,do mapping based on csv schemas
    direction_lookup: dict = {'N': 'NORTHBOUND', 'S': 'SOUTHBOUND', 'E': 'EASTBOUND',
                              'W': 'WESTBOUND'}
    carrier: str = 'COSU'
    for call_port in crawler_result:
        service_code: str = urlparse(str(call_port.url)).path[31:].split('.do')[0]
        for data in json.loads(call_port.read())['data']['content']:
            direction_code: str | None = str(data.get('direction', 'U'))[0].strip()
            direction: str = direction_lookup.get(direction_code, 'UNKNOWN')
            for port_sequence, ports in enumerate(data['ports'], start=0):
                port_name: str = str(ports.get('callPort')).upper().replace(' ', '')
                etd_day: str | None = str(ports.get('callPortEtd')).upper().strip() if str(
                    ports.get('callPortEtd')).isalpha() else str(ports.get('callPortEtdTime')).upper().strip()
                etd_time: str | None = ports.get('callPortEtdTime') if str(
                    ports.get('callPortEtdTime')).isnumeric() else ports.get('callPortEtd')
                eta_day: str | None = str(ports.get('callPortEta')).upper().strip() if str(
                    ports.get('callPortEta')).isalpha() else str(ports.get('callPortEtaTime')).upper().strip()
                eta_time: str | None = ports.get('callPortEtaTime') if str(
                    ports.get('callPortEtaTime')).isnumeric() else ports.get('callPortEta')
                common: dict = {'changeMode': None, 'allianceID': None, 'alliancePoolID': None,
                                'tradeID': None,
                                'oiServiceID': ''.join([service_code, carrier]),
                                'carrierID': carrier,
                                'serviceID': service_code + ' ' + ''.join(['[', direction_code, ']']),
                                'service': service_code,
                                'direction': direction,
                                'frequency': 'WEEKLY',
                                'portCode': port_name,
                                'relatedID': uuid.uuid5(uuid.NAMESPACE_DNS, f'{carrier}-{service_code}-{direction}')}
                if ports.get('callPortEtaTime') is not None:
                    pol: Services = Services(**common,
                                             startDay=eta_day.replace('TEU', 'THU').replace('WES', 'WED').replace(
                                                 'NONE', etd_day),
                                             tt=eta_time,
                                             order=order_counter(port_sequence, 'L'),
                                             locationType='L')
                    writer.writerow(pol.dict())
                if ports.get('callPortEtdTime') is not None:
                    pod: Services = Services(**common,
                                             startDay=etd_day.replace('TEU', 'THU').replace('WES', 'WED').replace(
                                                 'NONE', eta_day),
                                             tt=etd_time,
                                             order=order_counter(port_sequence, 'D'),
                                             locationType='D')
                    writer.writerow(pod.dict())


async def cosco_crawler():
    loop = asyncio.get_running_loop()
    start = time.perf_counter()
    csv_field_names: list = list(Services.schema()['properties'].keys())
    csv_result = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cosco_port_rotation.csv')
    with open(csv_result, mode="w", encoding="utf-8", newline='') as service_info:
        timeout = httpx.Timeout(50.0, read=None, connect=60.0)
        limits = httpx.Limits(max_keepalive_connections=60, max_connections=None)
        writer = csv.DictWriter(service_info, fieldnames=csv_field_names)
        writer.writeheader()
        logger.info("Created CSV header for COSCO")
        async with httpx.AsyncClient(verify=False, timeout=timeout, limits=limits) as client:
            # COSCO Service Groups
            service_groups = Crawler(
                client=client,
                sleep=None,
                urls=[settings.cosu_service_url.format(i) for i in
                      range(11, 19)],
                workers=5,
                limit=25,
            )
            await service_groups.run()
            service_groups_result = [{data.get('serLpGroupUuid'): data.get('serLpGroupNameEn')} for service_group in
                                     service_groups.result for data in
                                     json.loads(service_group.read())['data']['content']]

            services_seen = sorted(service_groups.seen)
            logger.info("Service Group Results:")
            for url in services_seen:
                logger.info(url)
            logger.info(f"Service Group Crawled: {len(service_groups.done)} URLs")
            logger.info(f"Service Group Processed: {len(services_seen)} URLs")

            # COSCO Route Services
            route_services = Crawler(
                client=client,
                sleep=None,
                urls=[settings.cosu_route_url.format(
                    next(iter(rs.keys())))
                    for rs in service_groups_result],
                workers=5,
                limit=25,
            )
            await route_services.run()

            route_services_result = [{data.get('serLpCode'): data.get('serLpNameEn')} for route_service in
                                     route_services.result for data in
                                     json.loads(route_service.read())['data']['content']]

            routes_seen = sorted(route_services.seen)
            logger.info("Route Services Results:")
            for url in routes_seen:
                logger.info(url)
            logger.info(f"Route Services Crawled: {len(route_services.done)} URLs")
            logger.info(f"Route Services Processed: {len(routes_seen)} URLs")

            # COSCO CALL PORTS
            call_ports = Crawler(
                client=client,
                sleep=None,
                urls=[settings.cosu_ports_url.format(next(iter(cp.keys())))
                      for cp in route_services_result],
                workers=5,
                limit=25,
            )
            await call_ports.run()

            # Using additional thread to speed up the entire processing for blockingIO task
            with concurrent.futures.ThreadPoolExecutor() as pool:
                result = await loop.run_in_executor(
                    pool, functools.partial(cosco_mapping, crawler_result=call_ports.result, writer=writer))

            call_port_seen = sorted(call_ports.seen)
            logger.info("Call Ports Results:")
            for url in call_port_seen:
                logger.info(url)
            logger.info(f"Call Ports Results Crawled: {len(call_ports.done)} URLs")
            logger.info(f"Call Ports Results Processed: {len(call_port_seen)} URLs")
            logger.info(f"Anything pending?: {result}")
        end = time.perf_counter()
        logger.info(f"Done in {end - start:.2f}s")

# asyncio.run(cosco(), debug=True)