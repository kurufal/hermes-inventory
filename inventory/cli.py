"""Argparse integration for the local Hermes Inventory secrets command."""

from inventory.commands import inventory_cli


def setup_inventory_cli(parser):
	"""Register `setup --secrets` on Hermes' inventory CLI parser."""
	subcommands = parser.add_subparsers(dest="inventory_command")
	setup = subcommands.add_parser("setup", help="Configure Hermes Inventory locally")
	setup.add_argument("--secrets", action="store_true", help="Prompt securely for the HomeBox API key")


def handle_inventory_cli(namespace):
	"""Dispatch Hermes' parsed argparse namespace without accepting key text."""
	if getattr(namespace, "inventory_command", None) == "setup" and getattr(namespace, "secrets", False):
		result = inventory_cli(["setup", "--secrets"])
	else:
		result = inventory_cli([])
	print(result)
	return result