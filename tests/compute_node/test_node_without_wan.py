"""Scenario C: WAN lost, Local Compute Node still reachable -> local execution succeeds.

Proves WAN connectivity != Local Compute health: the connectivity gate classifies
the node endpoint as LOCAL and never blocks it on WAN state.
"""
import asyncio

from node_support import ENDPOINT, PROJECT
from test_node_maker_product_loop import _loop, _request

from pados.connectivity.adapter import SimulatedConnectivity
from pados.connectivity.egress import EndpointLocality, WanEgressGate, endpoint_locality
from pados.connectivity.maker import ConnectivityGatedMaker
from pados.connectivity.state import ConnectivityMonitor, Link
from pados.product_loop.schemas import ProductLoopStatus


def test_local_node_executes_while_wan_offline(node, tmp_path):
    net = SimulatedConnectivity()
    net.go_offline()  # WAN and remote overlay down; LAN/loopback still up
    monitor = ConnectivityMonitor({Link.WAN: net.wan_probe, Link.REMOTE_TRANSPORT: net.overlay_probe})
    snapshot = monitor.observe()
    assert snapshot["links"]["WAN"]["state"] == "OFFLINE" and not monitor.wan_available()
    loop, gateway, workspace, _ = _loop(node, tmp_path)
    assert endpoint_locality(ENDPOINT) is EndpointLocality.LOCAL
    loop.maker = ConnectivityGatedMaker(
        loop.maker, endpoint=ENDPOINT,
        gate=WanEgressGate(wan_available=monitor.wan_available, endpoint_reachable=net.endpoint_reachable))
    report = asyncio.run(loop.execute(_request(workspace)))
    assert report.status == ProductLoopStatus.COMMITTED and gateway.versions[PROJECT] == 1
    assert report.final_result.execution_receipt["provider_id"] == "PADOS_LOCAL_INFERENCE"


def test_local_node_unreachable_on_lan_is_reported_not_rerouted(node, tmp_path):
    net = SimulatedConnectivity(lan=False)
    loop, gateway, workspace, _ = _loop(node, tmp_path)
    loop.maker = ConnectivityGatedMaker(
        loop.maker, endpoint=ENDPOINT, gate=WanEgressGate(wan_available=lambda: True,
                                                         endpoint_reachable=net.endpoint_reachable))
    report = asyncio.run(loop.execute(_request(workspace)))
    assert report.status != ProductLoopStatus.COMMITTED and not gateway.commits
    assert report.final_result.summary == "PROVIDER_UNREACHABLE"
    assert not node.runtime.jobs._jobs
