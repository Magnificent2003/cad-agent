"""Read-only health checks for the shared upstream FreeCAD RPC process."""
import asyncio
import xmlrpc.client


class Transport(xmlrpc.client.Transport):
    def make_connection(self, host):
        connection = super().make_connection(host)
        connection.timeout = 3
        return connection


def status():
    with xmlrpc.client.ServerProxy('http://127.0.0.1:9875', transport=Transport()) as rpc:
        return rpc.get_rpc_status()


async def idle():
    try:
        report = await asyncio.to_thread(status)
        return (report.get('success') is True and not report.get('async_jobs_running')
                and report.get('gui_dispatch', {}).get('state') == 'healthy')
    except (OSError, xmlrpc.client.Error):
        return False
