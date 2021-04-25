import time
from pathlib import Path
from queue import Queue
from tempfile import TemporaryFile
from typing import Optional

import grpc
import pytest
from mock import patch

from core.api.grpc import core_pb2
from core.api.grpc.clientw import CoreGrpcClient, InterfaceHelper, MoveNodesStreamer
from core.api.grpc.server import CoreGrpcServer
from core.api.grpc.wrappers import (
    ConfigOption,
    ConfigOptionType,
    EmaneModelConfig,
    Event,
    Geo,
    Hook,
    Interface,
    Link,
    LinkOptions,
    MobilityAction,
    Node,
    NodeServiceData,
    NodeType,
    Position,
    ServiceAction,
    ServiceConfig,
    ServiceFileConfig,
    ServiceValidationMode,
    SessionLocation,
    SessionState,
)
from core.api.tlv.dataconversion import ConfigShim
from core.api.tlv.enumerations import ConfigFlags
from core.emane.ieee80211abg import EmaneIeee80211abgModel
from core.emane.nodes import EmaneNet
from core.emulator.data import EventData, IpPrefixes, NodeData, NodeOptions
from core.emulator.enumerations import EventTypes, ExceptionLevels
from core.errors import CoreError
from core.location.mobility import BasicRangeModel, Ns2ScriptedMobility
from core.nodes.base import CoreNode
from core.nodes.network import SwitchNode, WlanNode
from core.xml.corexml import CoreXmlWriter


class TestGrpcw:
    def test_start_session(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        with client.context_connect():
            session_id = client.create_session()
            session = client.get_session(session_id)
        position = Position(x=50, y=100)
        node1 = Node(
            id=1, name="n1", position=position, type=NodeType.DEFAULT, model="PC"
        )
        position = Position(x=100, y=100)
        node2 = Node(
            id=2, name="n2", position=position, type=NodeType.DEFAULT, model="PC"
        )
        position = Position(x=200, y=200)
        wlan_node = Node(id=3, name="n3", type=NodeType.WIRELESS_LAN, position=position)
        session.set_node(node1)
        session.set_node(node2)
        session.set_node(wlan_node)
        iface_helper = InterfaceHelper(ip4_prefix="10.83.0.0/16")
        iface1_id = 0
        iface1 = iface_helper.create_iface(node1.id, iface1_id)
        iface2_id = 0
        iface2 = iface_helper.create_iface(node2.id, iface2_id)
        link = Link(node1_id=node1.id, node2_id=node2.id, iface1=iface1, iface2=iface2)
        session.links = [link]
        hook = Hook(state=SessionState.RUNTIME, file="echo.sh", data="echo hello")
        session.hooks = {hook.file: hook}
        location_x = 5
        location_y = 10
        location_z = 15
        location_lat = 20
        location_lon = 30
        location_alt = 40
        location_scale = 5
        session.location = SessionLocation(
            x=location_x,
            y=location_y,
            z=location_z,
            lat=location_lat,
            lon=location_lon,
            alt=location_alt,
            scale=location_scale,
        )

        # setup global emane config
        emane_config_key = "platform_id_start"
        emane_config_value = "2"
        option = ConfigOption(
            label=emane_config_key,
            name=emane_config_key,
            value=emane_config_value,
            type=ConfigOptionType.INT64,
            group="Default",
        )
        session.emane_config[emane_config_key] = option

        # setup wlan config
        wlan_config_key = "range"
        wlan_config_value = "333"
        option = ConfigOption(
            label=wlan_config_key,
            name=wlan_config_key,
            value=wlan_config_value,
            type=ConfigOptionType.INT64,
            group="Default",
        )
        wlan_node.wlan_config[wlan_config_key] = option

        # setup mobility config
        mobility_config_key = "refresh_ms"
        mobility_config_value = "60"
        option = ConfigOption(
            label=mobility_config_key,
            name=mobility_config_key,
            value=mobility_config_value,
            type=ConfigOptionType.INT64,
            group="Default",
        )
        wlan_node.mobility_config[mobility_config_key] = option

        # setup service config
        service_name = "DefaultRoute"
        service_validate = ["echo hello"]
        node1.service_configs[service_name] = NodeServiceData(
            executables=[],
            dependencies=[],
            dirs=[],
            configs=[],
            startup=[],
            validate=service_validate,
            validation_mode=ServiceValidationMode.NON_BLOCKING,
            validation_timer=0,
            shutdown=[],
            meta="",
        )

        # setup service file config
        service_file = "defaultroute.sh"
        service_file_data = "echo hello"
        node1.service_file_configs[service_name] = {service_file: service_file_data}

        # setup session option
        option_key = "controlnet"
        option_value = "172.16.0.0/24"
        option = ConfigOption(
            label=option_key,
            name=option_key,
            value=option_value,
            type=ConfigOptionType.STRING,
            group="Default",
        )
        session.options[option_key] = option

        # when
        with patch.object(CoreXmlWriter, "write"):
            with client.context_connect():
                client.start_session(session)

        # then
        real_session = grpc_server.coreemu.sessions[session.id]
        assert node1.id in real_session.nodes
        assert node2.id in real_session.nodes
        assert wlan_node.id in real_session.nodes
        assert iface1_id in real_session.nodes[node1.id].ifaces
        assert iface2_id in real_session.nodes[node2.id].ifaces
        hook_file, hook_data = real_session.hooks[EventTypes.RUNTIME_STATE][0]
        assert hook_file == hook.file
        assert hook_data == hook.data
        assert real_session.location.refxyz == (location_x, location_y, location_z)
        assert real_session.location.refgeo == (
            location_lat,
            location_lon,
            location_alt,
        )
        assert real_session.location.refscale == location_scale
        assert real_session.emane.get_config(emane_config_key) == emane_config_value
        set_wlan_config = real_session.mobility.get_model_config(
            wlan_node.id, BasicRangeModel.name
        )
        assert set_wlan_config[wlan_config_key] == wlan_config_value
        set_mobility_config = real_session.mobility.get_model_config(
            wlan_node.id, Ns2ScriptedMobility.name
        )
        assert set_mobility_config[mobility_config_key] == mobility_config_value
        service = real_session.services.get_service(
            node1.id, service_name, default_service=True
        )
        assert service.validate == tuple(service_validate)
        real_node1 = real_session.get_node(node1.id, CoreNode)
        service_file = real_session.services.get_service_file(
            real_node1, service_name, service_file
        )
        assert service_file.data == service_file_data
        assert option_value == real_session.options.get_config(option_key)

    @pytest.mark.parametrize("session_id", [None, 6013])
    def test_create_session(
        self, grpc_server: CoreGrpcServer, session_id: Optional[int]
    ):
        # given
        client = CoreGrpcClient()

        # when
        with client.context_connect():
            created_session_id = client.create_session(session_id)

        # then
        assert isinstance(created_session_id, int)
        session = grpc_server.coreemu.sessions.get(created_session_id)
        assert session is not None
        if session_id is not None:
            assert created_session_id == session_id
            assert session.id == session_id

    @pytest.mark.parametrize("session_id, expected", [(None, True), (6013, False)])
    def test_delete_session(
        self, grpc_server: CoreGrpcServer, session_id: Optional[int], expected: bool
    ):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        if session_id is None:
            session_id = session.id

        # then
        with client.context_connect():
            result = client.delete_session(session_id)

        # then
        assert result is expected
        assert grpc_server.coreemu.sessions.get(session_id) is None

    def test_get_session(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        session.add_node(CoreNode)
        session.set_state(EventTypes.DEFINITION_STATE)

        # then
        with client.context_connect():
            session = client.get_session(session.id)

        # then
        assert session.state == SessionState.DEFINITION
        assert len(session.nodes) == 1
        assert len(session.links) == 0

    def test_get_sessions(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()

        # then
        with client.context_connect():
            sessions = client.get_sessions()

        # then
        found_session = None
        for current_session in sessions:
            if current_session.id == session.id:
                found_session = current_session
                break
        assert len(sessions) == 1
        assert found_session is not None

    def test_get_session_location(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()

        # then
        with client.context_connect():
            location = client.get_session_location(session.id)

        # then
        assert location.scale == 1.0
        assert location.x == 0
        assert location.y == 0
        assert location.z == 0
        assert location.lat == 0
        assert location.lon == 0
        assert location.alt == 0

    def test_set_session_location(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        scale = 2
        xyz = (1, 1, 1)
        lat_lon_alt = (1, 1, 1)
        location = SessionLocation(
            xyz[0],
            xyz[1],
            xyz[2],
            lat_lon_alt[0],
            lat_lon_alt[1],
            lat_lon_alt[2],
            scale,
        )

        # then
        with client.context_connect():
            result = client.set_session_location(session.id, location)

        # then
        assert result is True
        assert session.location.refxyz == xyz
        assert session.location.refscale == scale
        assert session.location.refgeo == lat_lon_alt

    def test_set_session_metadata(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()

        # then
        key = "meta1"
        value = "value1"
        with client.context_connect():
            result = client.set_session_metadata(session.id, {key: value})

        # then
        assert result is True
        assert session.metadata[key] == value

    def test_get_session_metadata(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        key = "meta1"
        value = "value1"
        session.metadata[key] = value

        # then
        with client.context_connect():
            config = client.get_session_metadata(session.id)

        # then
        assert config[key] == value

    def test_set_session_state(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()

        # then
        with client.context_connect():
            result = client.set_session_state(session.id, SessionState.DEFINITION)

        # then
        assert result is True
        assert session.state == EventTypes.DEFINITION_STATE

    def test_add_node(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()

        # then
        with client.context_connect():
            position = Position(x=0, y=0)
            node = Node(id=1, name="n1", type=NodeType.DEFAULT, position=position)
            node_id = client.add_node(session.id, node)

        # then
        assert node_id is not None
        assert session.get_node(node_id, CoreNode) is not None

    def test_get_node(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        node = session.add_node(CoreNode)

        # then
        with client.context_connect():
            get_node, ifaces = client.get_node(session.id, node.id)

        # then
        assert node.id == get_node.id

    def test_edit_node(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        node = session.add_node(CoreNode)

        # then
        x, y = 10, 10
        with client.context_connect():
            position = Position(x=x, y=y)
            result = client.edit_node(session.id, node.id, position)

        # then
        assert result is True
        assert node.position.x == x
        assert node.position.y == y

    def test_edit_node_exception(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        node = session.add_node(CoreNode)

        # then
        x, y = 10, 10
        with client.context_connect():
            position = Position(x=x, y=y)
            geo = Geo(lat=0, lon=0, alt=0)
            with pytest.raises(CoreError):
                client.edit_node(session.id, node.id, position, geo=geo)

    @pytest.mark.parametrize("node_id, expected", [(1, True), (2, False)])
    def test_delete_node(
        self, grpc_server: CoreGrpcServer, node_id: int, expected: bool
    ):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        node = session.add_node(CoreNode)

        # then
        with client.context_connect():
            result = client.delete_node(session.id, node_id)

        # then
        assert result is expected
        if expected is True:
            with pytest.raises(CoreError):
                assert session.get_node(node.id, CoreNode)

    def test_node_command(self, request, grpc_server: CoreGrpcServer):
        if request.config.getoption("mock"):
            pytest.skip("mocking calls")

        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        session.set_state(EventTypes.CONFIGURATION_STATE)
        options = NodeOptions(model="Host")
        node = session.add_node(CoreNode, options=options)
        session.instantiate()
        expected_output = "hello world"

        # then
        command = f"echo {expected_output}"
        with client.context_connect():
            output = client.node_command(session.id, node.id, command)

        # then
        assert expected_output == output

    def test_get_node_terminal(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        session.set_state(EventTypes.CONFIGURATION_STATE)
        options = NodeOptions(model="Host")
        node = session.add_node(CoreNode, options=options)
        session.instantiate()

        # then
        with client.context_connect():
            terminal = client.get_node_terminal(session.id, node.id)

        # then
        assert terminal is not None

    def test_get_hooks(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        file_name = "test"
        file_data = "echo hello"
        session.add_hook(EventTypes.RUNTIME_STATE, file_name, file_data)

        # then
        with client.context_connect():
            hooks = client.get_hooks(session.id)

        # then
        assert len(hooks) == 1
        hook = hooks[0]
        assert hook.state == SessionState.RUNTIME
        assert hook.file == file_name
        assert hook.data == file_data

    def test_add_hook(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        hook = Hook(SessionState.RUNTIME, "test", "echo hello")

        # then
        with client.context_connect():
            result = client.add_hook(session.id, hook)

        # then
        assert result is True

    def test_save_xml(self, grpc_server: CoreGrpcServer, tmpdir: TemporaryFile):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        tmp = tmpdir.join("text.xml")

        # then
        with client.context_connect():
            client.save_xml(session.id, str(tmp))

        # then
        assert tmp.exists()

    def test_open_xml_hook(self, grpc_server: CoreGrpcServer, tmpdir: TemporaryFile):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        tmp = Path(tmpdir.join("text.xml"))
        session.save_xml(tmp)

        # then
        with client.context_connect():
            result, session_id = client.open_xml(tmp)

        # then
        assert result is True
        assert session_id is not None

    def test_get_node_links(self, grpc_server: CoreGrpcServer, ip_prefixes: IpPrefixes):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        switch = session.add_node(SwitchNode)
        node = session.add_node(CoreNode)
        iface_data = ip_prefixes.create_iface(node)
        session.add_link(node.id, switch.id, iface_data)

        # then
        with client.context_connect():
            links = client.get_node_links(session.id, switch.id)

        # then
        assert len(links) == 1

    def test_get_node_links_exception(
        self, grpc_server: CoreGrpcServer, ip_prefixes: IpPrefixes
    ):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        switch = session.add_node(SwitchNode)
        node = session.add_node(CoreNode)
        iface_data = ip_prefixes.create_iface(node)
        session.add_link(node.id, switch.id, iface_data)

        # then
        with pytest.raises(grpc.RpcError):
            with client.context_connect():
                client.get_node_links(session.id, 3)

    def test_add_link(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        switch = session.add_node(SwitchNode)
        node = session.add_node(CoreNode)
        assert len(switch.links()) == 0
        iface = InterfaceHelper("10.0.0.0/24").create_iface(node.id, 0)
        link = Link(node.id, switch.id, iface1=iface)

        # then
        with client.context_connect():
            result, iface1, _ = client.add_link(session.id, link)

        # then
        assert result is True
        assert len(switch.links()) == 1
        assert iface1.id == iface.id
        assert iface1.ip4 == iface.ip4

    def test_add_link_exception(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        node = session.add_node(CoreNode)

        # then
        link = Link(node.id, 3)
        with pytest.raises(grpc.RpcError):
            with client.context_connect():
                client.add_link(session.id, link)

    def test_edit_link(self, grpc_server: CoreGrpcServer, ip_prefixes: IpPrefixes):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        switch = session.add_node(SwitchNode)
        node = session.add_node(CoreNode)
        iface = ip_prefixes.create_iface(node)
        session.add_link(node.id, switch.id, iface)
        options = LinkOptions(bandwidth=30000)
        link = switch.links()[0]
        assert options.bandwidth != link.options.bandwidth
        link = Link(node.id, switch.id, iface1=Interface(id=iface.id), options=options)

        # then
        with client.context_connect():
            result = client.edit_link(session.id, link)

        # then
        assert result is True
        link = switch.links()[0]
        assert options.bandwidth == link.options.bandwidth

    def test_delete_link(self, grpc_server: CoreGrpcServer, ip_prefixes: IpPrefixes):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        node1 = session.add_node(CoreNode)
        iface1 = ip_prefixes.create_iface(node1)
        node2 = session.add_node(CoreNode)
        iface2 = ip_prefixes.create_iface(node2)
        session.add_link(node1.id, node2.id, iface1, iface2)
        link_node = None
        for node_id in session.nodes:
            node = session.nodes[node_id]
            if node.id not in {node1.id, node2.id}:
                link_node = node
                break
        assert len(link_node.links()) == 1
        link = Link(
            node1.id,
            node2.id,
            iface1=Interface(id=iface1.id),
            iface2=Interface(id=iface2.id),
        )

        # then
        with client.context_connect():
            result = client.delete_link(session.id, link)

        # then
        assert result is True
        assert len(link_node.links()) == 0

    def test_get_wlan_config(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        wlan = session.add_node(WlanNode)

        # then
        with client.context_connect():
            config = client.get_wlan_config(session.id, wlan.id)

        # then
        assert len(config) > 0

    def test_set_wlan_config(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        session.set_state(EventTypes.CONFIGURATION_STATE)
        wlan = session.add_node(WlanNode)
        wlan.setmodel(BasicRangeModel, BasicRangeModel.default_values())
        session.instantiate()
        range_key = "range"
        range_value = "50"

        # then
        with client.context_connect():
            result = client.set_wlan_config(
                session.id,
                wlan.id,
                {
                    range_key: range_value,
                    "delay": "0",
                    "loss": "0",
                    "bandwidth": "50000",
                    "error": "0",
                    "jitter": "0",
                },
            )

        # then
        assert result is True
        config = session.mobility.get_model_config(wlan.id, BasicRangeModel.name)
        assert config[range_key] == range_value
        assert wlan.model.range == int(range_value)

    def test_get_emane_config(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()

        # then
        with client.context_connect():
            config = client.get_emane_config(session.id)

        # then
        assert len(config) > 0

    def test_set_emane_config(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        config_key = "platform_id_start"
        config_value = "2"

        # then
        with client.context_connect():
            result = client.set_emane_config(session.id, {config_key: config_value})

        # then
        assert result is True
        config = session.emane.get_configs()
        assert len(config) > 1
        assert config[config_key] == config_value

    def test_get_emane_model_configs(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        session.set_location(47.57917, -122.13232, 2.00000, 1.0)
        options = NodeOptions(emane=EmaneIeee80211abgModel.name)
        emane_network = session.add_node(EmaneNet, options=options)
        session.emane.set_model(emane_network, EmaneIeee80211abgModel)
        config_key = "platform_id_start"
        config_value = "2"
        session.emane.set_model_config(
            emane_network.id, EmaneIeee80211abgModel.name, {config_key: config_value}
        )

        # then
        with client.context_connect():
            configs = client.get_emane_model_configs(session.id)

        # then
        assert len(configs) == 1
        model_config = configs[0]
        assert emane_network.id == model_config.node_id
        assert model_config.model == EmaneIeee80211abgModel.name
        assert len(model_config.config) > 0
        assert model_config.iface_id is None

    def test_set_emane_model_config(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        session.set_location(47.57917, -122.13232, 2.00000, 1.0)
        options = NodeOptions(emane=EmaneIeee80211abgModel.name)
        emane_network = session.add_node(EmaneNet, options=options)
        session.emane.set_model(emane_network, EmaneIeee80211abgModel)
        config_key = "bandwidth"
        config_value = "900000"
        option = ConfigOption(
            label=config_key,
            name=config_key,
            value=config_value,
            type=ConfigOptionType.INT32,
            group="Default",
        )
        config = EmaneModelConfig(
            emane_network.id, EmaneIeee80211abgModel.name, config={config_key: option}
        )

        # then
        with client.context_connect():
            result = client.set_emane_model_config(session.id, config)

        # then
        assert result is True
        config = session.emane.get_model_config(
            emane_network.id, EmaneIeee80211abgModel.name
        )
        assert config[config_key] == config_value

    def test_get_emane_model_config(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        session.set_location(47.57917, -122.13232, 2.00000, 1.0)
        options = NodeOptions(emane=EmaneIeee80211abgModel.name)
        emane_network = session.add_node(EmaneNet, options=options)
        session.emane.set_model(emane_network, EmaneIeee80211abgModel)

        # then
        with client.context_connect():
            config = client.get_emane_model_config(
                session.id, emane_network.id, EmaneIeee80211abgModel.name
            )

        # then
        assert len(config) > 0

    def test_get_emane_models(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()

        # then
        with client.context_connect():
            models = client.get_emane_models(session.id)

        # then
        assert len(models) > 0

    def test_get_mobility_configs(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        wlan = session.add_node(WlanNode)
        session.mobility.set_model_config(wlan.id, Ns2ScriptedMobility.name, {})

        # then
        with client.context_connect():
            configs = client.get_mobility_configs(session.id)

        # then
        assert len(configs) > 0
        assert wlan.id in configs
        config = configs[wlan.id]
        assert len(config) > 0

    def test_get_mobility_config(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        wlan = session.add_node(WlanNode)
        session.mobility.set_model_config(wlan.id, Ns2ScriptedMobility.name, {})

        # then
        with client.context_connect():
            config = client.get_mobility_config(session.id, wlan.id)

        # then
        assert len(config) > 0

    def test_set_mobility_config(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        wlan = session.add_node(WlanNode)
        config_key = "refresh_ms"
        config_value = "60"

        # then
        with client.context_connect():
            result = client.set_mobility_config(
                session.id, wlan.id, {config_key: config_value}
            )

        # then
        assert result is True
        config = session.mobility.get_model_config(wlan.id, Ns2ScriptedMobility.name)
        assert config[config_key] == config_value

    def test_mobility_action(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        wlan = session.add_node(WlanNode)
        session.mobility.set_model_config(wlan.id, Ns2ScriptedMobility.name, {})
        session.instantiate()

        # then
        with client.context_connect():
            result = client.mobility_action(session.id, wlan.id, MobilityAction.STOP)

        # then
        assert result is True

    def test_get_services(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()

        # then
        with client.context_connect():
            services = client.get_services()

        # then
        assert len(services) > 0

    def test_get_service_defaults(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()

        # then
        with client.context_connect():
            defaults = client.get_service_defaults(session.id)

        # then
        assert len(defaults) > 0

    def test_set_service_defaults(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        node_type = "test"
        services = ["SSH"]

        # then
        with client.context_connect():
            result = client.set_service_defaults(session.id, {node_type: services})

        # then
        assert result is True
        assert session.services.default_services[node_type] == services

    def test_get_node_service_configs(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        node = session.add_node(CoreNode)
        service_name = "DefaultRoute"
        session.services.set_service(node.id, service_name)

        # then
        with client.context_connect():
            services = client.get_node_service_configs(session.id)

        # then
        assert len(services) == 1
        service_config = services[0]
        assert service_config.node_id == node.id
        assert service_config.service == service_name

    def test_get_node_service(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        node = session.add_node(CoreNode)

        # then
        with client.context_connect():
            service = client.get_node_service(session.id, node.id, "DefaultRoute")

        # then
        assert len(service.configs) > 0

    def test_get_node_service_file(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        node = session.add_node(CoreNode)

        # then
        with client.context_connect():
            data = client.get_node_service_file(
                session.id, node.id, "DefaultRoute", "defaultroute.sh"
            )

        # then
        assert data is not None

    def test_set_node_service(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        node = session.add_node(CoreNode)
        config = ServiceConfig(node.id, "DefaultRoute", validate=["echo hello"])

        # then
        with client.context_connect():
            result = client.set_node_service(session.id, config)

        # then
        assert result is True
        service = session.services.get_service(
            node.id, config.service, default_service=True
        )
        assert service.validate == tuple(config.validate)

    def test_set_node_service_file(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        node = session.add_node(CoreNode)
        config = ServiceFileConfig(
            node.id, "DefaultRoute", "defaultroute.sh", "echo hello"
        )

        # then
        with client.context_connect():
            result = client.set_node_service_file(session.id, config)

        # then
        assert result is True
        service_file = session.services.get_service_file(
            node, config.service, config.file
        )
        assert service_file.data == config.data

    def test_service_action(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        node = session.add_node(CoreNode)
        service_name = "DefaultRoute"

        # then
        with client.context_connect():
            result = client.service_action(
                session.id, node.id, service_name, ServiceAction.STOP
            )

        # then
        assert result is True

    def test_node_events(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        node = session.add_node(CoreNode)
        node.position.lat = 10.0
        node.position.lon = 20.0
        node.position.alt = 5.0
        queue = Queue()

        def handle_event(event: Event) -> None:
            assert event.session_id == session.id
            assert event.node_event is not None
            event_node = event.node_event.node
            assert event_node.geo.lat == node.position.lat
            assert event_node.geo.lon == node.position.lon
            assert event_node.geo.alt == node.position.alt
            queue.put(event)

        # then
        with client.context_connect():
            client.events(session.id, handle_event)
            time.sleep(0.1)
            session.broadcast_node(node)

            # then
            queue.get(timeout=5)

    def test_link_events(self, grpc_server: CoreGrpcServer, ip_prefixes: IpPrefixes):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        wlan = session.add_node(WlanNode)
        node = session.add_node(CoreNode)
        iface = ip_prefixes.create_iface(node)
        session.add_link(node.id, wlan.id, iface)
        link_data = wlan.links()[0]
        queue = Queue()

        def handle_event(event: Event) -> None:
            assert event.session_id == session.id
            assert event.link_event is not None
            queue.put(event)

        # then
        with client.context_connect():
            client.events(session.id, handle_event)
            time.sleep(0.1)
            session.broadcast_link(link_data)

            # then
            queue.get(timeout=5)

    def test_throughputs(self, request, grpc_server: CoreGrpcServer):
        if request.config.getoption("mock"):
            pytest.skip("mocking calls")

        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        queue = Queue()

        def handle_event(event_data):
            assert event_data.session_id == session.id
            queue.put(event_data)

        # then
        with client.context_connect():
            client.throughputs(session.id, handle_event)
            time.sleep(0.1)

            # then
            queue.get(timeout=5)

    def test_session_events(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        queue = Queue()

        def handle_event(event: Event) -> None:
            assert event.session_id == session.id
            assert event.session_event is not None
            queue.put(event)

        # then
        with client.context_connect():
            client.events(session.id, handle_event)
            time.sleep(0.1)
            event_data = EventData(
                event_type=EventTypes.RUNTIME_STATE, time=str(time.monotonic())
            )
            session.broadcast_event(event_data)

            # then
            queue.get(timeout=5)

    def test_config_events(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        queue = Queue()

        def handle_event(event: Event) -> None:
            assert event.session_id == session.id
            assert event.config_event is not None
            queue.put(event)

        # then
        with client.context_connect():
            client.events(session.id, handle_event)
            time.sleep(0.1)
            session_config = session.options.get_configs()
            config_data = ConfigShim.config_data(
                0, None, ConfigFlags.UPDATE.value, session.options, session_config
            )
            session.broadcast_config(config_data)

            # then
            queue.get(timeout=5)

    def test_exception_events(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        queue = Queue()
        exception_level = ExceptionLevels.FATAL
        source = "test"
        node_id = None
        text = "exception message"

        def handle_event(event: Event) -> None:
            assert event.session_id == session.id
            assert event.exception_event is not None
            exception_event = event.exception_event
            assert exception_event.level.value == exception_level.value
            assert exception_event.node_id == 0
            assert exception_event.source == source
            assert exception_event.text == text
            queue.put(event)

        # then
        with client.context_connect():
            client.events(session.id, handle_event)
            time.sleep(0.1)
            session.exception(exception_level, source, text, node_id)

            # then
            queue.get(timeout=5)

    def test_file_events(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        node = session.add_node(CoreNode)
        queue = Queue()

        def handle_event(event: Event) -> None:
            assert event.session_id == session.id
            assert event.file_event is not None
            queue.put(event)

        # then
        with client.context_connect():
            client.events(session.id, handle_event)
            time.sleep(0.1)
            file_data = session.services.get_service_file(
                node, "DefaultRoute", "defaultroute.sh"
            )
            session.broadcast_file(file_data)

            # then
            queue.get(timeout=5)

    def test_move_nodes(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        node = session.add_node(CoreNode)
        x, y = 10.0, 15.0
        streamer = MoveNodesStreamer(session.id)
        streamer.send_position(node.id, x, y)
        streamer.stop()

        # then
        with client.context_connect():
            client.move_nodes(streamer)

        # assert
        assert node.position.x == x
        assert node.position.y == y

    def test_move_nodes_geo(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        node = session.add_node(CoreNode)
        lon, lat, alt = 10.0, 15.0, 5.0
        streamer = MoveNodesStreamer(session.id)
        streamer.send_geo(node.id, lon, lat, alt)
        streamer.stop()
        queue = Queue()

        def node_handler(node_data: NodeData):
            n = node_data.node
            assert n.position.lon == lon
            assert n.position.lat == lat
            assert n.position.alt == alt
            queue.put(node_data)

        session.node_handlers.append(node_handler)

        # then
        with client.context_connect():
            client.move_nodes(streamer)

        # assert
        assert queue.get(timeout=5)
        assert node.position.lon == lon
        assert node.position.lat == lat
        assert node.position.alt == alt

    def test_move_nodes_exception(self, grpc_server: CoreGrpcServer):
        # given
        client = CoreGrpcClient()
        session = grpc_server.coreemu.create_session()
        streamer = MoveNodesStreamer(session.id)
        request = core_pb2.MoveNodesRequest()
        streamer.send(request)
        streamer.stop()

        # then
        with pytest.raises(grpc.RpcError):
            with client.context_connect():
                client.move_nodes(streamer)