# Copyright 2016 Dravetech AB. All rights reserved.
#
# The contents of this file are licensed under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with the
# License. You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations under
# the License.

"""
Napalm driver for VyOS.

Read napalm.readthedocs.org for more information.


"""
import os
import re
import tempfile
import textfsm
import vyattaconfparser

import logging
logger = logging.getLogger("peering.manager.peering")

from django.core.cache import cache

cache.clear()

# NAPALM base
import napalm.base.constants as C
from napalm.base.base import NetworkDriver
from napalm.base.exceptions import (
    CommitError,
    ConnectionException,
    MergeConfigException,
    ReplaceConfigException,
)
from netmiko import ConnectHandler, SCPConn, __version__ as netmiko_version


class VyOSDriver(NetworkDriver):

    _MINUTE_SECONDS = 60
    _HOUR_SECONDS = 60 * _MINUTE_SECONDS
    _DAY_SECONDS = 24 * _HOUR_SECONDS
    _WEEK_SECONDS = 7 * _DAY_SECONDS
    _YEAR_SECONDS = 365 * _DAY_SECONDS
    _DEST_FILENAME = "/var/tmp/candidate_running.conf"
    _BACKUP_FILENAME = "/var/tmp/backup_running.conf"
    _BOOT_FILENAME = "/config/config.boot"

    def __init__(self, hostname, username, password, timeout=60, optional_args=None):
        self.hostname = hostname
        self.username = username
        self.password = password
        self.timeout = timeout
        self.device = None
        self._scp_client = None
        self._new_config = None
        self._old_config = None
        self._ssh_usekeys = False

        # Netmiko possible arguments
        netmiko_argument_map = {
            "port": None,
            "secret": "",
            "verbose": False,
            "global_delay_factor": 1,
            "use_keys": False,
            "key_file": None,
            "ssh_strict": False,
            "system_host_keys": False,
            "alt_host_keys": False,
            "alt_key_file": "",
            "ssh_config_file": None,
        }

        fields = netmiko_version.split(".")
        fields = [int(x) for x in fields]
        maj_ver, min_ver, bug_fix = fields
        if maj_ver >= 2 or maj_ver == 1 and min_ver >= 1:
            netmiko_argument_map["allow_agent"] = False
        # Build dict of any optional Netmiko args
        self.netmiko_optional_args = {}
        if optional_args is not None:
            for k in netmiko_argument_map:
                try:
                    self.netmiko_optional_args[k] = optional_args[k]
                except KeyError:
                    pass
            self.global_delay_factor = optional_args.get("global_delay_factor", 1)
            self.port = optional_args.get("port", 22)

    def open(self):
        self.device = ConnectHandler(
            device_type="vyos",
            host=self.hostname,
            username=self.username,
            password=self.password,
            **self.netmiko_optional_args,
        )

        try:
            self._scp_client = SCPConn(self.device)
        except:
            raise ConnectionException("Failed to open connection ")

    def close(self):
        self.device.disconnect()

    def is_alive(self):
        """Returns a flag with the state of the SSH connection."""
        return {"is_alive": self.device.remote_conn.transport.is_active()}

    def load_replace_candidate(self, filename=None, config=None):
        """
        Only configuration files are supported with load_replace_candidate.
        It must be a full config file like /config/config.boot
        Due to the OS nature,  we do not
        support a replace using a configuration string.
        """
        if not filename and not config:
            raise ReplaceConfigException("filename or config param must be provided.")

        if filename is None:
            temp_file = tempfile.NamedTemporaryFile(mode="w+")
            temp_file.write(config)
            temp_file.flush()
            cfg_filename = temp_file.name
        else:
            cfg_filename = filename

        if os.path.exists(cfg_filename) is not True:
            raise ReplaceConfigException("config file is not found")
        self._scp_client.scp_transfer_file(cfg_filename, self._DEST_FILENAME)
        self.device.send_command(
            f"cp {self._BOOT_FILENAME} {self._BACKUP_FILENAME}"
        )
        output_loadcmd = self.device.send_config_set(
            [f"load {self._DEST_FILENAME}"]
        )
        match_loaded = re.findall("Load complete.", output_loadcmd)
        match_notchanged = re.findall(
            "No configuration changes to commit", output_loadcmd
        )
        if match_failed := re.findall(
            "Failed to parse specified config file", output_loadcmd
        ):
            raise ReplaceConfigException(f"Failed replace config: {output_loadcmd}")

        if not match_loaded and not match_notchanged:
            raise ReplaceConfigException(f"Failed replace config: {output_loadcmd}")

    def load_merge_candidate(self, filename=None, config=None):
        """
        Only configuration in set-format is supported with load_merge_candidate.
        """

        if not filename and not config:
            raise MergeConfigException("filename or config param must be provided.")

        if filename is None:
            temp_file = tempfile.NamedTemporaryFile(mode="w+")
            temp_file.write(config)
            temp_file.flush()
            cfg_filename = temp_file.name
        else:
            cfg_filename = filename

        if os.path.exists(cfg_filename) is not True:
            raise MergeConfigException("config file is not found")
        with open(cfg_filename) as f:
            self.device.send_command(
                f"cp {self._BOOT_FILENAME} {self._BACKUP_FILENAME}"
            )
            self._new_config = f.read()
            cfg = [x for x in self._new_config.split("\n") if x]
            output_loadcmd = self.device.send_config_set(cfg)
            match_setfailed = re.findall("Delete failed", output_loadcmd)
            match_delfailed = re.findall("Set failed", output_loadcmd)

            if match_setfailed or match_delfailed:
                raise MergeConfigException(f"Failed merge config: {output_loadcmd}")

    def discard_config(self):
        self.device.exit_config_mode()

    def compare_config(self):
        output_compare = self.device.send_config_set(["compare"])
        if match := re.findall(
            "No changes between working and active configurations", output_compare
        ):
            return ""
        else:
            return "".join(output_compare.splitlines(True)[1:-1])

    def commit_config(self, message=""):
        if message:
            raise NotImplementedError(
                "Commit message not implemented for this platform"
            )

        try:
            self.device.commit()
        except ValueError as e:
            raise CommitError("Failed to commit config on the device") from e

        self.device.send_config_set(["save"])
        self.device.exit_config_mode()

    def rollback(self):
        """Rollback configuration to filename or to self.rollback_cfg file."""
        filename = None
        if filename is None:
            filename = self._BACKUP_FILENAME

            output_loadcmd = self.device.send_config_set([f"load {filename}"])
            if match := re.findall("Load complete.", output_loadcmd):
                self.device.send_config_set(["commit", "save"])
            else:
                raise ReplaceConfigException(
                    f"Failed rollback config: {output_loadcmd}"
                )

    def get_environment(self):
        """
        'vmstat' output:
        procs -----------memory---------- ---swap-- -----io---- -system-- ----cpu----
        r  b   swpd   free   buff  cache   si   so    bi    bo   in   cs us sy id wa
        0  0      0  61404 139624 139360    0    0     0     0    9   14  0  0 100  0
        """
        output_cpu_list = []
        output_cpu = self.device.send_command("vmstat")
        output_cpu = str(output_cpu)
        output_cpu_list = output_cpu.split("\n")
        if len(output_cpu_list[-1]) > 0:
            output_cpu_list = output_cpu_list[-1]
        else:
            output_cpu_list = output_cpu_list[-2]
        output_cpu_idle = output_cpu_list.split()[-2]
        cpu = 100 - int(output_cpu_idle)

        """
        'free' output:
                     total       used       free     shared    buffers     cached
        Mem:        508156     446784      61372          0     139624     139360
        -/+ buffers/cache:     167800     340356
        Swap:            0          0          0
        """
        output_ram = self.device.send_command("free").split("\n")[1]
        available_ram, used_ram = output_ram.split()[1:3]

        return {
            "fans": {"invalid": {"status": False}},
            "temperature": {
                "invalid": {
                    "temperature": 0.0,
                    "is_alert": False,
                    "is_critical": False,
                }
            },
            "power": {"invalid": {"status": True, "capacity": 0.0, "output": 0.0}},
            "cpu": {
                "0": {"%usage": float(cpu)},
            },
            "memory": {
                "available_ram": int(available_ram),
                "used_ram": int(used_ram),
            },
        }

    def get_interfaces(self):
        """
        "show interfaces" output example:
        Interface        IP Address                        S/L  Description
        ---------        ----------                        ---  -----------
        br0              -                                 u/D
        eth0             192.168.1.1/24                   u/u  Management
        eth1             192.168.1.2/24                    u/u
        eth2             192.168.3.1/24                    u/u  foobar
                         192.168.2.2/24
        lo               127.0.0.1/8                       u/u
                         ::1/128
        """
        output_iface = self.device.send_command("show interfaces")

        # Collect all interfaces' name and status
        match = re.findall(r"(\S+)\s+[:\-\d/\.]+\s+([uAD])/([uAD])", output_iface)

        # 'match' example:
        # [("br0", "u", "D"), ("eth0", "u", "u"), ("eth1", "u", "u")...]
        iface_state = {
            iface_name: {"State": state, "Link": link}
            for iface_name, state, link in match
        }

        output_conf = self.device.send_command("show configuration")

        # Convert the configuration to dictionary
        config = vyattaconfparser.parse_conf(output_conf)

        iface_dict = {}

        for iface_type in config["interfaces"]:

            ifaces_detail = config["interfaces"][iface_type]

            for iface_name in ifaces_detail:
                details = ifaces_detail[iface_name]
                description = details.get("description", "")
                speed = details.get("speed", "0")
                if speed == "auto":
                    speed = 0
                hw_id = details.get("hw-id", "00:00:00:00:00:00")

                is_up = iface_state[iface_name]["Link"] == "u"
                is_enabled = iface_state[iface_name]["State"] == "u"

                iface_dict[iface_name] = {
                    "is_up": is_up,
                    "is_enabled": is_enabled,
                    "description": description,
                    "last_flapped": float(-1),
                    "mtu": -1,
                    "speed": int(speed),
                    "mac_address": hw_id,
                }

        return iface_dict

    def get_arp_table(self, vrf=""):
        # 'age' is not implemented yet

        """
        'show arp' output example:
        Address                  HWtype  HWaddress           Flags Mask            Iface
        10.129.2.254             ether   00:50:56:97:af:b1   C                     eth0
        192.168.1.134                    (incomplete)                              eth1
        192.168.1.1              ether   00:50:56:ba:26:7f   C                     eth1
        10.129.2.97              ether   00:50:56:9f:64:09   C                     eth0
        192.168.1.3              ether   00:50:56:86:7b:06   C                     eth1
        """

        if vrf:
            raise NotImplementedError(
                "VRF support has not been added for this getter on this platform."
            )

        output = self.device.send_command("show arp")
        output = output.split("\n")

        # Skip the header line
        output = output[1:-1]

        arp_table = []
        for line in output:

            line = line.split()
            # 'line' example:
            # ["10.129.2.254", "ether", "00:50:56:97:af:b1", "C", "eth0"]
            # [u'10.0.12.33', u'(incomplete)', u'eth1']
            macaddr = "00:00:00:00:00:00" if "incomplete" in line[1] else line[2]
            arp_table.append(
                {
                    "interface": line[-1],
                    "mac": macaddr,
                    "ip": line[0],
                    "age": 0.0,
                }
            )

        return arp_table

    def get_ntp_stats(self):
        """
        'ntpq -np' output example
             remote           refid      st t when poll reach   delay   offset  jitter
        ==============================================================================
         116.91.118.97   133.243.238.244  2 u   51   64  377    5.436  987971. 1694.82
         219.117.210.137 .GPS.            1 u   17   64  377   17.586  988068. 1652.00
         133.130.120.204 133.243.238.164  2 u   46   64  377    7.717  987996. 1669.77
        """

        output = self.device.send_command("ntpq -np")
        output = output.split("\n")[2:]
        ntp_stats = []

        for ntp_info in output:
            if len(ntp_info) > 0:
                (
                    remote,
                    refid,
                    st,
                    t,
                    when,
                    hostpoll,
                    reachability,
                    delay,
                    offset,
                    jitter,
                ) = ntp_info.split()

                # 'remote' contains '*' if the machine synchronized with NTP server
                synchronized = "*" in remote

                match = re.search(r"(\d+\.\d+\.\d+\.\d+)", remote)
                ip = match[1]

                when = when if when != "-" else 0

                ntp_stats.append(
                    {
                        "remote": ip,
                        "referenceid": refid,
                        "synchronized": synchronized,
                        "stratum": int(st),
                        "type": t,
                        "when": when,
                        "hostpoll": int(hostpoll),
                        "reachability": int(reachability),
                        "delay": float(delay),
                        "offset": float(offset),
                        "jitter": float(jitter),
                    }
                )

        return ntp_stats

    def get_ntp_peers(self):
        output = self.device.send_command("ntpq -np")
        output_peers = output.split("\n")[2:]
        ntp_peers = {}

        for line in output_peers:
            if len(line) > 0:
                match = re.search(r"(\d+\.\d+\.\d+\.\d+)\s+", line)
                ntp_peers[match[1]] = {}

        return ntp_peers

    def get_bgp_neighbors(self):
        # 'description', 'sent_prefixes' and 'received_prefixes' are not implemented yet

        """
        'show ip bgp summary' output example:
        BGP router identifier 192.168.1.2, local AS number 64520
        IPv4 Unicast - max multipaths: ebgp 1 ibgp 1
        RIB entries 3, using 288 bytes of memory
        Peers 3, using 13 KiB of memory

        Neighbor        V    AS MsgRcvd MsgSent   TblVer  InQ OutQ Up/Down  State/PfxRcd
        192.168.1.1     4 64519    7226    7189        0    0    0 4d23h40m        1
        192.168.1.3     4 64521    7132    7103        0    0    0 4d21h05m        0
        192.168.1.4     4 64522       0       0        0    0    0 never    Active
        """

        output = self.device.send_command("show ip bgp summary")

        current_dir = os.path.dirname(os.path.abspath(__file__))
        template_path = os.path.join(current_dir, "templates", "bgp_sum.template")

        # Assuming you've got a TextFSM template ready to parse the `bgp_detail` output
        with open(template_path) as template_file:
            fsm = textfsm.TextFSM(template_file)
            header = fsm.header
            result = fsm.ParseText(output)

        bgp_neighbor_data = {"global": {"router_id": "", "peers": {}}}

        bgp_neighbor_data["global"]["router_id"] = result[0][
            header.index("BGP_ROUTER_ID")
        ]

        for neighbor in result:
            peer_id = neighbor[header.index("NEIGHBOR")]
            bgp_neighbor_data["global"]["peers"][peer_id] = []

            peer_dict = {
                "description": str(neighbor[header.index("DESCRIPTION")]),
                "is_enabled": "Admin" not in neighbor[header.index("PREFIX_SENT")],
                "local_as": int(neighbor[header.index("LOCAL_AS")]),
                "is_up": "Active"
                not in neighbor[header.index("STATE_PREFIX_RECEIVED")],
                "remote_id": neighbor[header.index("NEIGHBOR")],
                "remote_address": neighbor[header.index("NEIGHBOR")],
                "uptime": int(
                    self._bgp_time_conversion(neighbor[header.index("UP_TIME")])
                ),
                "remote_as": int(neighbor[header.index("NEIGHBOR_AS")]),
            }

            bgp_neighbor_data["global"]["peers"][peer_id] = peer_dict

        return bgp_neighbor_data

    def get_bgp_neighbors_detail(self, neighbor_address=""):

        def safe_int(value, default=0):
            try:
                return int(value) if value and value.isdigit() else default
            except ValueError:
                return default

        bgp_neighbor_data = {"global": {}}

        neighbors = self.get_bgp_neighbors()

        for neighbor in neighbors["global"]["peers"]:

            output = self.device.send_command(f"show ip bgp neighbor {neighbor}")

            current_dir = os.path.dirname(os.path.abspath(__file__))
            template_path = os.path.join(
                current_dir, "templates", "bgp_details.template"
            )

            with open(template_path) as template_file:
                fsm = textfsm.TextFSM(template_file)
                result = fsm.ParseText(output)

                if not result:
                    continue

                neighbors_dicts = [
                    dict(zip(fsm.header, neighbor)) for neighbor in result
                ]

                for neighbor_detail in neighbors_dicts:

                    remote_as = neighbor_detail["REMOTE_AS"]
                    logger.debug(f"Parsing AS {remote_as} for neighbor {neighbor}")

                    peer_dict = {
                        "up": neighbor_detail["BGP_STATE"].lower() == "established",
                        "local_as": int(neighbor_detail["LOCAL_AS"]),
                        "remote_as": int(neighbor_detail["REMOTE_AS"]),
                        "router_id": neighbor_detail["LOCAL_ROUTER_ID"],
                        "local_address": neighbor_detail[
                            "LOCAL_ROUTER_ID"
                        ],  # Adjusted from LOCAL_ROUTER_ID based on context
                        "routing_table": f"IPv{neighbor_detail['BGP_VERSION']} Unicast",  # Constructed value
                        "local_address_configured": bool(neighbor_detail["LOCAL_ROUTER_ID"]),
                        "local_port": (
                            int(neighbor_detail["LOCAL_PORT"])
                            if neighbor_detail["LOCAL_PORT"].isdigit()
                            else None
                        ),
                        "remote_address": neighbor,
                        "remote_port": neighbor_detail["FOREIGN_PORT"],
                        "multipath": neighbor_detail.get(
                            "DYNAMIC_CAPABILITY", "no"
                        ),  # Assuming DYNAMIC_CAPABILITY indicates multipath
                        "remove_private_as": (
                            "yes"
                            if neighbor_detail.get("REMOVE_PRIVATE_AS", "no") != "no"
                            else "no"
                        ),  # Placeholder for actual value
                        "input_messages": sum(
                            int(neighbor_detail["MESSAGE_STATISTICS_RECEIVED"][i])
                            for i in range(len(neighbor_detail["MESSAGE_STATISTICS_TYPE"]))
                            if neighbor_detail["MESSAGE_STATISTICS_TYPE"][i]
                            in ["Updates", "Keepalives"]
                        ),
                        "output_messages": sum(
                            int(neighbor_detail["MESSAGE_STATISTICS_SENT"][i])
                            for i in range(len(neighbor_detail["MESSAGE_STATISTICS_TYPE"]))
                            if neighbor_detail["MESSAGE_STATISTICS_TYPE"][i]
                            in ["Updates", "Keepalives"]
                        ),
                        "input_updates": safe_int(
                            neighbor_detail.get("RECEIVED_PREFIXES_IPV4")
                        )
                        + safe_int(neighbor_detail.get("RECEIVED_PREFIXES_IPV6")),
                        "output_updates": safe_int(
                            neighbor_detail.get("ADVERTISED_PREFIX_COUNT")
                        ),
                        "connection_state": neighbor_detail["BGP_STATE"].lower().strip(','),
                        "bgp_state": neighbor_detail["BGP_STATE"].lower().strip(','),
                        "previous_connection_state": neighbor_detail.get(
                            "LAST_RESET_REASON", "unknown"
                        ),
                        "last_event": neighbor_detail.get(
                            "LAST_EVENT", "Not Available"
                        ),  # Assuming LAST_EVENT is available
                        "suppress_4byte_as": neighbor_detail.get(
                            "FOUR_BYTE_AS_CAPABILITY", "Not Configured"
                        ),
                        "local_as_prepend": neighbor_detail.get(
                            "LOCAL_AS_PREPEND", "Not Configured"
                        ),  # Assuming LOCAL_AS_PREPEND is available
                        "holdtime": int(neighbor_detail["HOLD_TIME"]),
                        "configured_holdtime": int(neighbor_detail["CONFIGURED_HOLD_TIME"]),
                        "keepalive": int(neighbor_detail["KEEPALIVE_INTERVAL"]),
                        "configured_keepalive": int(
                            neighbor_detail["CONFIGURED_KEEPALIVE_INTERVAL"]
                        ),
                        "active_prefix_count": int(
                            neighbor_detail.get("ACTIVE_PREFIX_COUNT", 0)
                        ),  # Assuming ACTIVE_PREFIX_COUNT is available
                        "accepted_prefix_count": int(
                            neighbor_detail.get("ACCEPTED_PREFIX_COUNT", 0)
                        ),  # Assuming ACCEPTED_PREFIX_COUNT is available
                        "suppressed_prefix_count": int(
                            neighbor_detail.get("SUPPRESSED_PREFIX_COUNT", 0)
                        ),  # Assuming SUPPRESSED_PREFIX_COUNT is available
                        "advertised_prefix_count": int(
                            neighbor_detail.get("ADVERTISED_PREFIX_COUNT", 0)
                        ),
                        "received_prefix_count": safe_int(
                            neighbor_detail.get("RECEIVED_PREFIXES_IPV4", 0)
                        )
                        + safe_int(neighbor_detail.get("RECEIVED_PREFIXES_IPV6", 0)),
                        "flap_count": safe_int(
                            neighbor_detail.get("FLAP_COUNT", 0)
                        ),  # Assuming FLAP_COUNT is available
                    }

                    bgp_neighbor_data["global"].setdefault(int(remote_as), []).append(
                        peer_dict
                    )
                    logger.debug("Connection state: " + neighbor_detail["BGP_STATE"].lower().strip(','))


        return bgp_neighbor_data

    def _bgp_time_conversion(self, bgp_uptime):
        if "never" in bgp_uptime:
            return -1
        if "y" in bgp_uptime:
            match = re.search(r"(\d+)(\w)(\d+)(\w)(\d+)(\w)", bgp_uptime)
            return (
                int(match[1]) * self._YEAR_SECONDS
                + int(match[3]) * self._WEEK_SECONDS
                + int(match[5]) * self._DAY_SECONDS
            )
        elif "w" in bgp_uptime:
            match = re.search(r"(\d+)(\w)(\d+)(\w)(\d+)(\w)", bgp_uptime)
            return (
                int(match[1]) * self._WEEK_SECONDS
                + int(match[3]) * self._DAY_SECONDS
            ) + int(match[5]) * self._HOUR_SECONDS
        elif "d" in bgp_uptime:
            match = re.search(r"(\d+)(\w)(\d+)(\w)(\d+)(\w)", bgp_uptime)
            return (
                (int(match.group(1)) * self._DAY_SECONDS)
                + (int(match.group(3)) * self._HOUR_SECONDS)
                + (int(match.group(5)) * self._MINUTE_SECONDS)
            )
        else:
            hours, minutes, seconds = map(int, bgp_uptime.split(":"))
            return (
                (hours * self._HOUR_SECONDS)
                + (minutes * self._MINUTE_SECONDS)
                + seconds
            )

    def get_lldp_neighbors(self):
        # Multiple neighbors per port are not implemented
        # The show lldp neighbors commands lists port descriptions, not IDs
        output = self.device.send_command("show lldp neighbors detail")
        pattern = r"""(?s)Interface: +(?P<interface>\S+), [^\n]+
.+?
 +SysName: +(?P<hostname>\S+)
.+?
 +PortID: +ifname (?P<port>\S+)"""

        def _get_interface(match):
            return [
                {
                    "hostname": match.group("hostname"),
                    "port": match.group("port"),
                }
            ]

        return {
            match.group("interface"): _get_interface(match)
            for match in re.finditer(pattern, output)
        }

    def get_interfaces_counters(self):
        # 'rx_unicast_packet', 'rx_broadcast_packets', 'tx_unicast_packets',
        # 'tx_multicast_packets' and 'tx_broadcast_packets' are not implemented yet

        """
        'show interfaces detail' output example:
        eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc pfifo_fast state
        UP group default qlen 1000
        link/ether 00:50:56:86:8c:26 brd ff:ff:ff:ff:ff:ff
        ~~~
        RX:  bytes    packets     errors    dropped    overrun      mcast
          35960043     464584          0        221          0        407
        TX:  bytes    packets     errors    dropped    carrier collisions
          32776498     279273          0          0          0          0
        """
        output = self.device.send_command("show interfaces detail")
        interfaces = re.findall(r"(\S+): <.*", output)
        # count = re.findall("(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+", output)
        count = re.findall(r"(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)", output)
        counters = {}

        for j, i in enumerate(count):
            if j % 2 == 0:
                rx_errors = i[2]
                rx_discards = i[3]
                rx_octets = i[0]
                rx_unicast_packets = i[1]
                rx_multicast_packets = i[5]
                rx_broadcast_packets = -1
            else:
                counters[interfaces[j // 2]] = {
                    "tx_errors": int(i[2]),
                    "tx_discards": int(i[3]),
                    "tx_octets": int(i[0]),
                    "tx_unicast_packets": int(i[1]),
                    "tx_multicast_packets": -1,
                    "tx_broadcast_packets": -1,
                    "rx_errors": int(rx_errors),
                    "rx_discards": int(rx_discards),
                    "rx_octets": int(rx_octets),
                    "rx_unicast_packets": int(rx_unicast_packets),
                    "rx_multicast_packets": int(rx_multicast_packets),
                    "rx_broadcast_packets": int(rx_broadcast_packets),
                }
        return counters

    def get_snmp_information(self):
        # 'acl' is not implemented yet

        output = self.device.send_command("show configuration")
        # convert the configuration to dictionary
        config = vyattaconfparser.parse_conf(output)

        snmp = {"community": {}}
        try:
            for i in config["service"]["snmp"]["community"]:
                snmp["community"].update(
                    {
                        i: {
                            "acl": "",
                            "mode": config["service"]["snmp"]["community"][i][
                                "authorization"
                            ],
                        }
                    }
                )

            snmp |= {
                "chassis_id": "",
                "contact": config["service"]["snmp"]["contact"],
                "location": config["service"]["snmp"]["location"],
            }

            return snmp
        except KeyError:
            return {}

    def get_facts(self):
        output_uptime = self.device.send_command("cat /proc/uptime | awk '{print $1}'")

        uptime = int(float(output_uptime))

        output = self.device.send_command("show version").split("\n")
        ver_str = [line for line in output if "Version" in line][0]
        version = self.parse_version(ver_str)

        above_1_1 = not version.startswith("1.0") and not version.startswith("1.1")
        if above_1_1:
            sn_str = [line for line in output if "Hardware S/N" in line][0]
            hwmodel_str = [line for line in output if "Hardware model" in line][0]
        else:
            sn_str = [line for line in output if "S/N" in line][0]
            hwmodel_str = [line for line in output if "HW model" in line][0]

        snumber = self.parse_snumber(sn_str)
        hwmodel = self.parse_hwmodel(hwmodel_str)

        output = self.device.send_command("show configuration")
        config = vyattaconfparser.parse_conf(output)

        if "host-name" in config["system"]:
            hostname = config["system"]["host-name"]
        else:
            hostname = None

        if "domain-name" in config["system"]:
            fqdn = config["system"]["domain-name"]
        else:
            fqdn = ""

        iface_list = []
        for iface_type in config["interfaces"]:
            for iface_name in config["interfaces"][iface_type]:
                iface_list.append(iface_name)

        facts = {
            "uptime": int(uptime),
            "vendor": "VyOS",
            "os_version": version,
            "serial_number": snumber,
            "model": hwmodel,
            "hostname": hostname,
            "fqdn": fqdn,
            "interface_list": iface_list,
        }

        return facts

    @staticmethod
    def parse_version(ver_str):
        return ver_str.split()[-1]

    @staticmethod
    def parse_snumber(sn_str):
        sn = sn_str.split(":")
        return sn[1].strip()

    @staticmethod
    def parse_hwmodel(model_str):
        model = model_str.split(":")
        return model[1].strip()

    def get_interfaces_ip(self):
        output = self.device.send_command("show interfaces")
        output = output.split("\n")

        # delete the header line and the interfaces which has no ip address
        if len(output[-1]) > 0:
            ifaces = [x for x in output[3:] if "-" not in x]
        else:
            ifaces = [x for x in output[3:-1] if "-" not in x]

        ifaces_ip = {}

        for iface in ifaces:
            iface = iface.split()
            if len(iface) != 1:

                iface_name = iface[0]

                # Delete the "Interface" column
                iface = iface[1:-1]
                # Key initialization
                ifaces_ip[iface_name] = {}

            ip_addr, mask = iface[0].split("/")
            ip_ver = self._get_ip_version(ip_addr)

            # Key initialization
            if ip_ver not in ifaces_ip[iface_name]:
                ifaces_ip[iface_name][ip_ver] = {}

            ifaces_ip[iface_name][ip_ver][ip_addr] = {"prefix_length": int(mask)}

        return ifaces_ip

    @staticmethod
    def _get_ip_version(ip_address):
        if ":" in ip_address:
            return "ipv6"
        elif "." in ip_address:
            return "ipv4"

    def get_users(self):
        output = self.device.send_command("show configuration commands").split("\n")

        user_conf = [x.split() for x in output if "login user" in x]

        # Collect all users' name
        user_name = list({x[4] for x in user_conf})

        user_auth = {}

        for user in user_name:
            sshkeys = []

            # extract the configuration which relates to 'user'
            for line in [x for x in user_conf if user in x]:

                # "set system login user alice authentication encrypted-password 'abc'"
                if line[6] == "encrypted-password":
                    password = line[7].strip("'")

                elif line[5] == "level":
                    level = 15 if line[6].strip("'") == "admin" else 0
                elif len(line) == 10 and line[8] == "key":
                    sshkeys.append(line[9].strip("'"))

            user_auth[user] = {"level": level, "password": password, "sshkeys": sshkeys}

        return user_auth

    def ping(
        self,
        destination,
        source=C.PING_SOURCE,
        ttl=C.PING_TTL,
        timeout=C.PING_TIMEOUT,
        size=C.PING_SIZE,
        count=C.PING_COUNT,
        vrf=C.PING_VRF,
    ):
        # does not support multiple destination yet

        deadline = timeout * count

        command = f"ping {destination} "
        command += "ttl %d " % ttl
        command += "deadline %d " % deadline
        command += "size %d " % size
        command += "count %d " % count
        if source != "":
            command += f"interface {source} "

        ping_result = {}
        output_ping = self.device.send_command(command)

        err = "Unknown host" if "Unknown host" in output_ping else ""
        if err:
            ping_result["error"] = err
        else:
            # 'packet_info' example:
            # ['5', 'packets', 'transmitted,' '5', 'received,' '0%', 'packet',
            # 'loss,', 'time', '3997ms']
            packet_info = output_ping.split("\n")

            packet_info = (
                packet_info[-2] if len(packet_info[-1]) > 0 else packet_info[-3]
            )
            packet_info = [x.strip() for x in packet_info.split()]

            sent = int(packet_info[0])
            received = int(packet_info[3])
            lost = sent - received

            # 'rtt_info' example:
            # ["0.307/0.396/0.480/0.061"]
            rtt_info = output_ping.split("\n")

            rtt_info = rtt_info[-1] if len(rtt_info[-1]) > 0 else rtt_info[-2]
            match = re.search(r"([\d\.]+)/([\d\.]+)/([\d\.]+)/([\d\.]+)", rtt_info)

            if match is not None:
                rtt_min = float(match[1])
                rtt_avg = float(match[2])
                rtt_max = float(match[3])
                rtt_stddev = float(match[4])
            else:
                rtt_min = None
                rtt_avg = None
                rtt_max = None
                rtt_stddev = None

            ping_result["success"] = {}
            ping_result["success"] = {
                "probes_sent": sent,
                "packet_loss": lost,
                "rtt_min": rtt_min,
                "rtt_max": rtt_max,
                "rtt_avg": rtt_avg,
                "rtt_stddev": rtt_stddev,
                "results": [{"ip_address": destination, "rtt": rtt_avg}],
            }

            return ping_result

    def get_config(self, retrieve="all", full=False, sanitized=False):
        """
        Return the configuration of a device.
        :param retrieve: String to determine which configuration type you want to retrieve, default is all of them.
                              The rest will be set to "".
        :param full: Boolean to retrieve all the configuration. (Not supported)
        :param sanitized: Boolean to remove secret data. (Only supported for 'running')
        :return: The object returned is a dictionary with a key for each configuration store:
            - running(string) - Representation of the native running configuration
            - candidate(string) - Representation of the candidate configuration.
            - startup(string) - Representation of the native startup configuration.
        """
        if retrieve not in ["running", "candidate", "startup", "all"]:
            raise Exception(
                "ERROR: Not a valid option to retrieve.\nPlease select from 'running', 'candidate', "
                "'startup', or 'all'"
            )
        else:
            config_dict = {"running": "", "startup": "", "candidate": ""}
            if retrieve in ["running", "all"]:
                config_dict["running"] = self._get_running_config(sanitized)
            if retrieve in ["startup", "all"]:
                config_dict["startup"] = self.device.send_command(
                    f"cat {self._BOOT_FILENAME}"
                )
            if retrieve in ["candidate", "all"]:
                config_dict["candidate"] = self._new_config or ""

        return config_dict

    def _get_running_config(self, sanitized):
        if sanitized:
            return self.device.send_command("show configuration")
        self.device.config_mode()
        config = self.device.send_command("show")
        config = config[: config.rfind("\n")]
        self.device.exit_config_mode()
        return config
