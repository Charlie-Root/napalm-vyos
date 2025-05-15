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
try:
    import logging
    logger = logging.getLogger("peering.manager.peering")

    from django.core.cache import cache

    cache.clear()
except Exception:
    pass
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

    def get_bgp_neighbors(self):
        """
        Get BGP neighbors information.
        """
        bgp_neighbor_data = {"global": {"router_id": "", "peers": {}}}

        output = self.device.send_command("show bgp summary")

        current_dir = os.path.dirname(os.path.abspath(__file__))
        template_path = os.path.join(current_dir, "templates", "bgp_sum.template")

        # Parse using TextFSM template
        with open(template_path) as template_file:
            fsm = textfsm.TextFSM(template_file)
            result = fsm.ParseText(output)

            if not result:
                return bgp_neighbor_data

            # Set router ID from first result
            bgp_neighbor_data["global"]["router_id"] = result[0][fsm.header.index("BGP_ROUTER_ID")]

            # Process each neighbor
            for neighbor in result:
                peer_id = neighbor[fsm.header.index("NEIGHBOR")]
                state_prefix = neighbor[fsm.header.index("STATE_PREFIX_RECEIVED")]
                
                # Skip if we've already processed this peer
                if peer_id in bgp_neighbor_data["global"]["peers"]:
                    continue

                # Determine address family
                address_family = "ipv6" if ":" in peer_id else "ipv4"

                # Parse prefix counts
                received_prefixes = 0
                if state_prefix.isdigit():
                    received_prefixes = int(state_prefix)
                elif not any(x in state_prefix for x in ["Idle", "Active", "Connect"]):
                    # Try to extract number if it's in a different format
                    match = re.search(r"(\d+)", state_prefix)
                    if match:
                        received_prefixes = int(match.group(1))

                peer_dict = {
                    "description": str(neighbor[fsm.header.index("DESCRIPTION")]).strip(),
                    "is_enabled": "Admin" not in state_prefix,
                    "local_as": int(neighbor[fsm.header.index("LOCAL_AS")]),
                    "is_up": not any(x in state_prefix for x in ["Idle", "Active", "Connect"]),
                    "remote_id": peer_id,
                    "remote_address": peer_id,
                    "uptime": int(self._bgp_time_conversion(neighbor[fsm.header.index("UP_TIME")])),
                    "remote_as": int(neighbor[fsm.header.index("NEIGHBOR_AS")]),
                    "address_family": {
                        address_family: {
                            "received_prefixes": received_prefixes,
                            "accepted_prefixes": received_prefixes,  # In VyOS, received = accepted
                            "sent_prefixes": int(neighbor[fsm.header.index("PREFIX_SENT")])
                        }
                    }
                }

                bgp_neighbor_data["global"]["peers"][peer_id] = peer_dict

        return bgp_neighbor_data

    # Rest of the VyOSDriver class implementation...