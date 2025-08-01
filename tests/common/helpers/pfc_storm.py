import logging
import os
import re
import json

from jinja2 import Template
from tests.common.errors import MissingInputError
from tests.common.devices.sonic import SonicHost

TEMPLATES_DIR = os.path.realpath((os.path.join(os.path.dirname(__file__), "../../common/templates")))
ANSIBLE_ROOT = os.path.realpath((os.path.join(os.path.dirname(__file__), "../../../ansible")))
RUN_PLAYBOOK = os.path.realpath(os.path.join(os.path.dirname(__file__), "../../scripts/exec_template.yml"))

logger = logging.getLogger(__name__)


def get_chip_name_if_asic_pfc_storm_supported(fanout):
    hwSkuInfo = {
        "Arista DCS-7060DX5": "Tomahawk4",
        "Arista DCS-7060PX5": "Tomahawk4",
        "Arista DCS-7060X6": "Tomahawk5",
        "Arista-7060X6": "Tomahawk5",
        "Arista DCS-7060CX": "Tomahawk",
        "Arista-7060CX": "Tomahawk",
        "Arista DCS-7260CX3": "Tomahawk2",
        "Arista-7260CX3": "Tomahawk2",
        "Arista-7260QX3": "Tomahawk2",
        }

    for sku, chip in hwSkuInfo.items():
        if fanout.startswith(sku):
            return chip

    return None


class PFCStorm(object):
    """ PFC storm/start on different interfaces on a fanout connected to the DUT"""

    _PFC_GEN_DIR = {
        'sonic': '/tmp',
        'eos': '/mnt/flash',
    }

    def __init__(self, duthost, fanout_graph_facts, fanouthosts, **kwargs):
        """
        Args:
            duthost(AnsibleHost) : dut instance
            fanout_graph_facts(dict) : fixture that returns the fanouts connection info
            fanouthosts(AnsibleHost) : fanout instance
            kwargs(dict):
                peer_info(dict): keys are 'peerdevice', 'pfc_fanout_interface'. Optional: 'hwsku'
                pfc_gen_chip_name(string) : chip name of switch where PFC frames are generated. default: None
                pfc_queue_index(int) : queue on which the PFC storm should be generated. default: 3
                pfc_frames_number(int) : Number of PFC frames to generate. default: 100000
                pfc_gen_file(string): Script which generates the PFC traffic. default: 'pfc_gen.py'
                Other keys: 'pfc_storm_defer_time', 'pfc_storm_stop_defer_time', 'pfc_asym'
        """
        self.dut = duthost
        self.asic_type = duthost.facts['asic_type']
        hostvars = self.dut.host.options['variable_manager']._hostvars[self.dut.hostname]
        self.inventory = hostvars['inventory_file'].split('/')[-1]
        self.ip_addr = duthost.mgmt_ip
        self.fanout_info = fanout_graph_facts
        self.fanout_hosts = fanouthosts
        self.pfc_gen_file = kwargs.pop('pfc_gen_file', "pfc_gen.py")
        self.pfc_gen_multiprocess = kwargs.pop('pfc_gen_multiprocess', False)
        self.pfc_gen_chip_name = None
        self.pfc_queue_idx = kwargs.pop('pfc_queue_index', 3)
        self.pfc_frames_number = kwargs.pop('pfc_frames_number', 100000)
        self.send_pfc_frame_interval = kwargs.pop('send_pfc_frame_interval', 0)
        self.pfc_send_period = kwargs.pop('pfc_send_period', None)
        self.peer_info = kwargs.pop('peer_info')
        self._validate_params(expected_args=['pfc_fanout_interface', 'peerdevice'])
        if 'hwsku' not in self.peer_info:
            self._populate_peer_hwsku()
        self.platform_name = None
        self.update_platform_name()
        self._populate_optional_params(kwargs)
        if self.asic_type == 'vs':
            self.peer_device = {}
            self.fanout_asic_type = ""
        else:
            self.peer_device = self.fanout_hosts[self.peer_info['peerdevice']]
            self.fanout_asic_type = self.peer_device.facts['asic_type'] \
                if isinstance(self.peer_device.host, SonicHost) else None

    def _populate_peer_hwsku(self):
        """
        Find out the hwsku associated with the fanout
        """
        if self.asic_type == 'vs':
            return
        peer_dev_info = self.fanout_info[self.peer_info['peerdevice']]['device_info']
        self.peer_info['hwsku'] = peer_dev_info['HwSku']

    def _validate_params(self, **params):
        """
        Validate if all the needed keys are present
        """
        if self.asic_type == 'vs':
            return
        expected_args = params.get('expected_args')
        peer_info_keys = list(self.peer_info.keys())
        if not all(elem in peer_info_keys for elem in expected_args):
            raise MissingInputError("Peer_info does not contain all the keys,"
                                    "Expected args: {}".format(expected_args))

    def _populate_optional_params(self, kwargs):
        """
        Create var and assign values if any the following keys are present
        'pfc_storm_defer_time', 'pfc_storm_stop_defer_time', 'pfc_asym'
        """
        if len(kwargs) > 0:
            self.__dict__.update(kwargs)
        kwargs.clear()

    def _generate_mellanox_lable_port_map(self):
        """
        In SONiC the port alias contains mellanox label port
        For example, 'etp20' contains label port '20'
        This method is used to generate port name and label port map from fanout
        """
        port_name_label_port_map = {}
        show_int_status = self.fanout_hosts[self.peer_info['peerdevice']].host.show_and_parse("show interface status")
        port_name_alias_map = {intf["interface"]: intf['alias'] for intf in show_int_status}
        for port_name, alias in port_name_alias_map.items():
            label_port = re.findall(r'\d+[a-z]?', alias)
            port_name_label_port_map[port_name] = label_port[0]
        return port_name_label_port_map

    def _generate_mellanox_label_ports(self):
        label_port_map = self._generate_mellanox_lable_port_map()
        port_names = self.peer_info['pfc_fanout_interface']
        label_ports = [label_port_map[port_name] for port_name in port_names.split(',')]

        return ",".join(label_ports)

    def _create_pfc_gen(self):
        """
        Create the pfc generation file on the fanout if it does not exist
        """
        if self.asic_type == 'vs':
            return
        pfc_gen_fpath = os.path.join(self._PFC_GEN_DIR[self.peer_device.os],
                                     self.pfc_gen_file)
        out = self.peer_device.stat(path=pfc_gen_fpath)
        if not out['stat']['exists'] or not out['stat']['isdir']:
            self.peer_device.file(path=pfc_gen_fpath, state="touch")

    def _get_eos_fanout_version(self):
        """
        Get version info for eos fanout device
        """
        cmd = 'Cli -c "show version"'
        return self.peer_device.shell(cmd)['stdout_lines']

    def _get_sonic_fanout_hwsku(self):
        """
        Get hwsku for sonic fanout device
        """
        cmd = 'show version'
        out_lines = self.peer_device.shell(cmd)['stdout_lines']
        for line in out_lines:
            if line.startswith('HwSKU:'):
                return line.split()[1]

    def deploy_pfc_gen(self):
        """
        Deploy the pfc generation file on the fanout
        """
        if self.asic_type == 'vs':
            return
        if self.peer_device.os in ('eos', 'sonic'):
            chip_name = None
            if self.peer_device.os == 'eos':
                chip_name = get_chip_name_if_asic_pfc_storm_supported(self._get_eos_fanout_version()[0])
            elif self.peer_device.os == 'sonic':
                chip_name = get_chip_name_if_asic_pfc_storm_supported(self._get_sonic_fanout_hwsku())
            if self.peer_device.os in ('eos', 'sonic') and chip_name:
                self.pfc_gen_file = "pfc_gen_brcm_xgs.py"
                self.pfc_gen_file_test_name = "pfc_gen_brcm_xgs.py"
                self.pfc_gen_chip_name = chip_name
            src_pfc_gen_file = "common/helpers/{}".format(self.pfc_gen_file)
            self._create_pfc_gen()
            if self.fanout_asic_type == 'mellanox':
                src_pfc_gen_file = f"../ansible/roles/test/files/mlnx/docker-tests-pfcgen-asic/{self.pfc_gen_file}"
            self.peer_device.copy(
                src=src_pfc_gen_file,
                dest=self._PFC_GEN_DIR[self.peer_device.os]
                )
            if self.fanout_asic_type == 'mellanox':
                cmd = f"docker cp {self._PFC_GEN_DIR[self.peer_device.os]}/{self.pfc_gen_file} syncd:/root/"
                self.peer_device.shell(cmd)

    def update_queue_index(self, q_idx):
        """
        Update the queue index. Can be invoked after the class init to change the queue index
        """
        self.pfc_queue_idx = q_idx

    def update_peer_info(self, peer_info):
        """
        Update the fanout info. Can be invoked after the class init to change the fanout or fanout interface
        """
        if self.asic_type == 'vs':
            return
        self._validate_params(expected_args=['peerdevice', 'pfc_fanout_interface'])
        for key in peer_info:
            self.peer_info[key] = peer_info[key]
        if 'hwsku' not in peer_info:
            self._populate_peer_hwsku()
        self.update_platform_name()
        self.peer_device = self.fanout_hosts[self.peer_info['peerdevice']]

    def update_platform_name(self):
        """
        Identifies the fanout platform
        """
        if self.asic_type == 'vs':
            return
        if 'arista' in self.peer_info['hwsku'].lower():
            self.platform_name = 'arista'
        elif 'MLNX-OS' in self.peer_info['hwsku']:
            self.platform_name = 'mlnx'

    def _update_template_args(self):
        """
        Populates all the vars needed by the pfc storm templates
        """
        self.extra_vars = dict()
        self.extra_vars = {
            "pfc_gen_file": self.pfc_gen_file,
            "pfc_queue_index": self.pfc_queue_idx,
            "pfc_frames_number": self.pfc_frames_number,
            "pfc_fanout_interface": self.peer_info['pfc_fanout_interface'] if self.asic_type != 'vs' else "",
            "pfc_gen_chip_name": self.pfc_gen_chip_name,
            "ansible_eth0_ipv4_addr": self.ip_addr,
            "peer_hwsku": self.peer_info['hwsku'] if self.asic_type != 'vs' else "",
            "send_pfc_frame_interval": self.send_pfc_frame_interval,
            "pfc_send_period": self.pfc_send_period
            }
        if getattr(self, "pfc_storm_defer_time", None):
            self.extra_vars.update({"pfc_storm_defer_time": self.pfc_storm_defer_time})
        if getattr(self, "pfc_storm_stop_defer_time", None):
            self.extra_vars.update({"pfc_storm_stop_defer_time": self.pfc_storm_stop_defer_time})
        if getattr(self, "pfc_asym", None):
            self.extra_vars.update({"pfc_asym": self.pfc_asym})
        if self.asic_type == "mellanox":
            self.extra_vars.update({"pfc_gen_multiprocess": True})

        if self.asic_type != 'vs':
            if self.peer_device.os in self._PFC_GEN_DIR:
                self.extra_vars['pfc_gen_dir'] = self._PFC_GEN_DIR[self.peer_device.os]
            if self.fanout_asic_type == 'mellanox' and self.peer_device.os == 'sonic':
                self.extra_vars.update({"pfc_fanout_label_port": self._generate_mellanox_label_ports()})

    def _prepare_start_template(self):
        """
        Populates the pfc storm start template
        """
        self._update_template_args()
        if self.asic_type == 'vs':
            self.pfc_start_template = os.path.join(
                TEMPLATES_DIR, "pfc_storm_eos.j2")
        elif self.dut.topo_type == 't2' and self.peer_device.os == 'sonic':
            self.pfc_start_template = os.path.join(
                TEMPLATES_DIR, "pfc_storm_{}_t2.j2".format(self.peer_device.os))
        elif self.fanout_asic_type == 'mellanox' and self.peer_device.os == 'sonic':
            self.pfc_start_template = os.path.join(
                TEMPLATES_DIR, "pfc_storm_mlnx_{}.j2".format(self.peer_device.os))
        elif ((self.peer_device.os == 'eos' and
               get_chip_name_if_asic_pfc_storm_supported(self._get_eos_fanout_version()[0])) or
              (self.peer_device.os == 'sonic' and
               get_chip_name_if_asic_pfc_storm_supported(self._get_sonic_fanout_hwsku()))):
            self.pfc_start_template = os.path.join(
                TEMPLATES_DIR, "pfc_storm_arista_{}.j2".format(self.peer_device.os))
        else:
            self.pfc_start_template = os.path.join(
                TEMPLATES_DIR, "pfc_storm_{}.j2".format(self.peer_device.os))
        self.extra_vars.update({"template_path": self.pfc_start_template})

    def _prepare_stop_template(self):
        """
        Populates the pfc storm stop template
        """
        self._update_template_args()
        if self.asic_type == 'vs':
            self.pfc_stop_template = os.path.join(
                TEMPLATES_DIR, "pfc_storm_stop_eos.j2")
        elif self.dut.topo_type == 't2' and self.peer_device.os == 'sonic':
            self.pfc_stop_template = os.path.join(
                TEMPLATES_DIR, "pfc_storm_stop_{}_t2.j2".format(self.peer_device.os))
        elif self.fanout_asic_type == 'mellanox' and self.peer_device.os == 'sonic':
            self.pfc_stop_template = os.path.join(
                TEMPLATES_DIR, "pfc_storm_stop_mlnx_{}.j2".format(self.peer_device.os))
        elif ((self.peer_device.os == 'eos' and
               get_chip_name_if_asic_pfc_storm_supported(self._get_eos_fanout_version()[0])) or
              (self.peer_device.os == 'sonic' and
               get_chip_name_if_asic_pfc_storm_supported(self._get_sonic_fanout_hwsku()))):
            self.pfc_stop_template = os.path.join(
                TEMPLATES_DIR, "pfc_storm_stop_arista_{}.j2".format(self.peer_device.os))
        else:
            self.pfc_stop_template = os.path.join(
                TEMPLATES_DIR, "pfc_storm_stop_{}.j2".format(self.peer_device.os))
        self.extra_vars.update({"template_path": self.pfc_stop_template})

    def _run_pfc_gen_template(self):
        """
        Run pfc generator script on a specific OS type.
        """
        if self.asic_type == 'vs':
            return
        if self.peer_device.os == 'sonic':
            with open(self.extra_vars['template_path']) as tmpl_fd:
                tmpl = Template(tmpl_fd.read())
                cmds = tmpl.render(**self.extra_vars).splitlines()
            cmds = (_.strip() for _ in cmds)
            cmd = "; ".join(_ for _ in cmds if _)
            logger.info("Running command: {}".format(cmd))
            self.peer_device.shell(cmd, module_ignore_errors=True)
        else:
            # TODO: replace this playbook execution with Mellanox
            # onyx_config/onyx_command modules
            logger.info("Running Template: {}".format(json.dumps(self.extra_vars)))

            self.peer_device.exec_template(
                ANSIBLE_ROOT, RUN_PLAYBOOK,
                self.inventory, **self.extra_vars
                )

    def start_storm(self):
        """
        Starts PFC storm on the fanout interfaces
        """
        self._prepare_start_template()
        if self.asic_type == 'vs':
            return
        logger.info("--- Starting PFC storm on {} on interfaces {} on queue {} ---"
                    .format(self.peer_info['peerdevice'],
                            self.peer_info['pfc_fanout_interface'],
                            self.pfc_queue_idx))
        self._run_pfc_gen_template()

    def stop_storm(self):
        """
        Stops PFC storm on the fanout interfaces
        """
        self._prepare_stop_template()
        if self.asic_type == 'vs':
            return
        logger.info("--- Stopping PFC storm on {} on interfaces {} on queue {} ---"
                    .format(self.peer_info['peerdevice'],
                            self.peer_info['pfc_fanout_interface'],
                            self.pfc_queue_idx))
        self._run_pfc_gen_template()


class PFCMultiStorm(object):
    """ PFC storm start/stop on multiple fanouts connected to the DUT"""
    def __init__(self, duthost, fanout_graph_facts, fanouthosts, peer_params):
        """
        Args:
            duthost(AnsibleHost) : dut instance
            fanout_graph_facts(dict) : fixture that returns the fanouts connection info
            fanouthosts(AnsibleHost) : fanout instance
            peer_params(dict) : contains all the params needed for pfc storm
               eg. peer_params = {'peerdevice':
                                     {'pfc_gen_file': pfc_gen_file,
                                      'pfc_frames_number': frame count sent on all intf in the inf_list,
                                      'pfc_queue_index': q_index for the pfc storm on all intf in the intf list,
                                      'intfs': [intf_1, intf_2]
                                     }
                                 }
            pfc_queue_index(int) : queue on which the PFC storm should be generated. default: 4
            pfc_frames_number(int) : Number of PFC frames to generate. default: 100000000
            pfc_gen_file(string): Script which generates the PFC traffic. default: pfc_gen.py
            storm_handle(dict): PFCStorm instance for each fanout connected to the DUT
        """
        self.duthost = duthost
        self.asic_type = duthost.facts['asic_type']
        self.fanout_graph = fanout_graph_facts
        self.fanouthosts = fanouthosts
        self.peer_params = peer_params
        self.pfc_queue_index = 4
        self.pfc_frames_number = 100000000
        self.pfc_gen_file = "pfc_gen.py"
        self.storm_handle = dict()

    def _get_pfc_params(self, peer_dev):
        """
        Populate the pfc params value with the ones in peer_params dict if available

        Args:
            peer_dev(string): fanout name

        Returns:
            q_idx(int): PFC queue where PFC storm should be generated on that fanout
            frames_cnt(int): Number of PFC frames to be sent from the fanout
            gen_file(string): Name of pfc storm generation script
        """
        q_idx = self.pfc_queue_index
        frames_cnt = self.pfc_frames_number
        gen_file = self.pfc_gen_file
        if 'pfc_frames_number' in self.peer_params[peer_dev]:
            frames_cnt = self.peer_params[peer_dev]['pfc_frames_number']
        if 'pfc_queue_index' in self.peer_params[peer_dev]:
            q_idx = self.peer_params[peer_dev]['pfc_queue_index']
        if 'pfc_gen_file' in self.peer_params[peer_dev]:
            gen_file = self.peer_params[peer_dev]['pfc_gen_file']
        return q_idx, frames_cnt, gen_file

    def set_storm_params(self):
        """
        Construct the peer info and deploy the pfc gen script on the fanouts
        """
        for peer_dev in self.peer_params:
            if self.asic_type == 'vs':
                peer_info = {}
            else:
                peer_dev_info = self.fanout_graph[peer_dev]['device_info']
                peer_info = {'peerdevice': peer_dev,
                             'hwsku': peer_dev_info['HwSku'],
                             'pfc_fanout_interface': self.peer_params[peer_dev]['intfs']}

            q_idx, frames_cnt, gen_file = self._get_pfc_params(peer_dev)
            if self.duthost.topo_type == 't2' and self.fanouthosts[peer_dev].os == 'sonic':
                gen_file = 'pfc_gen_t2.py'
                pfc_send_time = 60
            else:
                gen_file = 'pfc_gen.py'
                pfc_send_time = None
            # get pfc storm handle
            self.storm_handle[peer_dev] = PFCStorm(self.duthost, self.fanout_graph,
                                                   self.fanouthosts,
                                                   pfc_queue_index=q_idx,
                                                   pfc_frames_number=frames_cnt,
                                                   pfc_gen_file=gen_file,
                                                   pfc_send_period=pfc_send_time,
                                                   peer_info=peer_info)

            self.storm_handle[peer_dev].deploy_pfc_gen()

    def start_pfc_storm(self):
        """
        Start PFC storm on all fanouts connected to the DUT
        """
        for hndle in self.storm_handle:
            self.storm_handle[hndle].start_storm()

    def stop_pfc_storm(self):
        """
        Stop PFC storm on all fanouts connected to the DUT
        """
        for hndle in self.storm_handle:
            self.storm_handle[hndle].stop_storm()
