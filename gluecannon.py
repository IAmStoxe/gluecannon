import os
import sys
import yaml
import logging
import subprocess
import argparse
from jinja2 import Environment, FileSystemLoader

COMPOSE_FILE = "docker-compose.yml"
ENV_FILE = ".env"
CONFIG_FILE = "config.yml"
HAPROXY_CONFIG_FILE = "haproxy.cfg"
DEFAULT_PROXY_PORT = 8888

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

env = Environment(loader=FileSystemLoader(os.path.join(os.path.dirname(__file__), 'templates')))

def load_config(file_path: str = CONFIG_FILE) -> dict:
    try:
        with open(file_path, "r") as file:
            return yaml.safe_load(file)
    except FileNotFoundError:
        logging.error(f"Configuration file not found: {file_path}")
        sys.exit(1)
    except yaml.YAMLError as e:
        logging.error(f"Error reading configuration file: {e}")
        sys.exit(1)

def build_env_list(provider_key: str, required_env: dict, optional_env: dict) -> list:
    provider_name = provider_key.replace('_', ' ').lower()
    env_list = [f"VPN_SERVICE_PROVIDER={provider_name}"]
    for key, value in required_env.items():
        env_list.append(f"{key}={value}")
    for key, value in optional_env.items():
        env_list.append(f"{key}={value}")
    return env_list

def generate_compose_file(config: dict, file_path: str = COMPOSE_FILE):
    global_config = config.get("global_settings", {})
    proxy_port = global_config.get("proxy_port", DEFAULT_PROXY_PORT)
    image = global_config.get("image", "default_image")
    services = {}
    for provider_key, provider in config.get("vpn_providers", {}).items():
        for i in range(provider.get("num_containers", 1)):
            service_name = f"{provider_key}_{i}"
            required_env = provider.get("required_env", {})
            optional_env = provider.get("optional_env", {})
            services[service_name] = {
                "container_name": service_name,
                "image": image,
                "cap_add": ["NET_ADMIN"],
                "devices": ["/dev/net/tun"],
                "env_file": ENV_FILE,
                "environment": build_env_list(provider_key, required_env, optional_env),
                "volumes": ["gluetun:/gluetun"],
                "logging": {
                    "driver": "json-file",
                    "options": {"max-size": "10m", "max-file": "3"},
                },
                "restart": "always",
                "networks": ["vpn-network"],
            }

    template = env.get_template('docker-compose.yml.j2')
    compose_content = template.render(services=services, proxy_port=proxy_port, haproxy_config_file=HAPROXY_CONFIG_FILE)
    with open(file_path, "w") as file:
        file.write(compose_content)
    logging.info(f"Generated {file_path} with {len(services)} services")

def generate_haproxy_config(config: dict, file_path: str = HAPROXY_CONFIG_FILE):
    all_services = {
        provider_key: {
            f"{provider_key}_{i}": f"{provider_key}_{i}:8888"
            for i in range(provider["num_containers"])
        }
        for provider_key, provider in config.get("vpn_providers", {}).items()
    }
    global_config = config.get("global_settings", {})
    proxy_port = global_config.get("proxy_port", DEFAULT_PROXY_PORT)
    template = env.get_template('haproxy.cfg.j2')
    haproxy_config = template.render(proxy_port=proxy_port, all_services=all_services)
    with open(file_path, "w") as file:
        file.write(haproxy_config)
    logging.info(f"Generated {file_path}")

def run_docker_compose_command(command: list):
    try:
        result = subprocess.run(
            ["docker", "compose", "-f", COMPOSE_FILE] + command,
            capture_output=True, text=True, check=True
        )
        logging.info(result.stdout)
        return result.stdout
    except subprocess.CalledProcessError as e:
        logging.error(f"Failed to run docker compose command '{' '.join(command)}': {e}")
        logging.error(e.stdout)
        sys.exit(1)

def manage_containers(action: str, config: dict):

    if action == "up":
        running_containers = run_docker_compose_command(["ps", "-q"]).strip()
        if running_containers:
            logging.info("Containers already running, restarting them.")
            run_docker_compose_command(["down", "-v"])
        # Configs needs to be generated after stopping the containers, and before starting the new ones
        generate_compose_file(config)
        generate_haproxy_config(config)
        run_docker_compose_command(["up", "-d"])
        logging.info("Started or restarted VPN containers and HAProxy")
    elif action == "down":
        run_docker_compose_command(["down"])
        logging.info("Stopped VPN containers and HAProxy")

def list_containers():
    services = run_docker_compose_command(["ps", "--services"]).split()
    logging.info(f"Services: {', '.join(services)}")
    return services

def run_command_through_proxy(cmd: list, config: dict):
    proxy_port = config["global_settings"]["proxy_port"]
    full_cmd = ["exec", "-e", f"ALL_PROXY=socks5h://haproxy:{proxy_port}", "haproxy"] + cmd
    run_docker_compose_command(full_cmd)
    logging.info(f"Ran command through HAProxy: {' '.join(cmd)}")

def start_interactive_shell(config: dict):
    proxy_port = config["global_settings"]["proxy_port"]
    cmd = ["exec", "-e", f"ALL_PROXY=socks5h://haproxy:{proxy_port}", "haproxy", "sh"]
    logging.info(f"Starting interactive shell through HAProxy on port {proxy_port}")
    run_docker_compose_command(cmd)

def parse_arguments():
    parser = argparse.ArgumentParser(description="VPN container orchestration script")
    parser.add_argument(
        "action",
        choices=["up", "down", "list", "run", "interactive"],
        help="Action to perform"
    )
    parser.add_argument(
        "cmd",
        nargs="*",
        help="Command to run through the proxy (only required for 'run' action)"
    )
    args = parser.parse_args()
    if args.action == "run" and not args.cmd:
        parser.error("The 'run' action requires a command to be specified.")
    return args

def main():
    config = load_config()
    args = parse_arguments()
    command_methods = {
        "up": lambda: manage_containers("up", config),
        "down": lambda: manage_containers("down", config),
        "list": list_containers,
        "run": lambda: run_command_through_proxy(args.cmd, config),
        "interactive": lambda: start_interactive_shell(config),
    }
    if args.action in command_methods:
        command_methods[args.action]()
    else:
        logging.error(f"Unknown command: {args.action}")
        sys.exit(1)

if __name__ == "__main__":
    main()
