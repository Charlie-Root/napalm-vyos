# Update the get_bgp_neighbors method
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

            peer_dict = {
                "description": str(neighbor[fsm.header.index("DESCRIPTION")]),
                "is_enabled": "Admin" not in state_prefix,
                "local_as": int(neighbor[fsm.header.index("LOCAL_AS")]),
                "is_up": not any(x in state_prefix for x in ["Idle", "Active", "Connect"]),
                "remote_id": peer_id,
                "remote_address": peer_id,
                "uptime": int(self._bgp_time_conversion(neighbor[fsm.header.index("UP_TIME")])),
                "remote_as": int(neighbor[fsm.header.index("NEIGHBOR_AS")]),
                "address_family": {
                    "ipv4" if ":" not in peer_id else "ipv6": {
                        "received_prefixes": int(state_prefix) if state_prefix.isdigit() else 0,
                        "accepted_prefixes": int(state_prefix) if state_prefix.isdigit() else 0,
                        "sent_prefixes": int(neighbor[fsm.header.index("PREFIX_SENT")])
                    }
                }
            }

            bgp_neighbor_data["global"]["peers"][peer_id] = peer_dict

    return bgp_neighbor_data