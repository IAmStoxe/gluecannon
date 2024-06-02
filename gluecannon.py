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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

env = Environment(loader=FileSystemLoader(os.path.dirname(__file__)))

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

def build_env_list(provider_key: str, required_env: list, optional_env: list) -> list:
    provider_name = provider_key.replace('_', ' ').lower()
    env_list = [f"VPN_SERVICE_PROVIDER={provider_name}"]
    env_list += required_env
    env_list += optional_env
    return env_list

def generate_compose_file(config: dict, file_path: str = COMPOSE_FILE):
    global_config = config.get("global_settings", {})
    proxy_port = global_config.get("proxy_port", DEFAULT_PROXY_PORT)
    image = global_config.get("image", "default_image")
    services = {}
    for provider_key, provider in config.get("vpn_providers", {}).items():
        for i in range(provider.get("num_containers", 1)):
            service_name = f"{provider_key}_{i}"
            required_env = provider.get("required_env", [])
            optional_env = provider.get("optional_env", [])
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

    template = env.from_string("""
version: '3.8'
services:
{% for service_name, service in services.items() %}
  {{ service_name }}:
    container_name: {{ service.container_name }}
    image: {{ service.image }}
    cap_add:
      - NET_ADMIN
    devices:
      - /dev/net/tun
    env_file: {{ service.env_file }}
    environment:
      - HTTPPROXY=on
{% for env in service.environment %}
      - {{ env }}
{% endfor %}
    volumes:
      - gluetun:/gluetun
    logging:
      driver: json-file
      options:
        max-size: 10m
        max-file: 3
    restart: always
    ports:
        - "8888/tcp"
        - "8080/tcp"
    networks:
      - vpn-network
{% endfor %}
  haproxy:
    container_name: haproxy-container
    image: haproxy:3.0
    ports:
      - "{{ proxy_port }}:{{ proxy_port }}/tcp"
      - "8080:8080/tcp"
    depends_on:
{% for dep in services.keys() %}
      - {{ dep }}
{% endfor %}
    restart: always
    volumes:
      - ./{{ haproxy_config_file }}:/usr/local/etc/haproxy/haproxy.cfg
    networks:
      - vpn-network
volumes:
  gluetun:
networks:
  vpn-network:
""")

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
    template = env.from_string("""
# HAProxy configuration template
global
    log 127.0.0.1 local0
    maxconn 4096
defaults
    log global
    mode http
    option httplog
    timeout connect 5000
    timeout client 50000
    timeout server 50000
frontend http-in
    bind *:{{ proxy_port }}
    default_backend vpn-backends
backend vpn-backends
    mode http
    balance roundrobin
    {% for provider, services in all_services.items() %}
    {% for service_name, service_address in services.items() %}
    server {{ service_name }} {{ service_address }} check
    {% endfor %}
    {% endfor %}
""")
    haproxy_config = template.render(proxy_port=proxy_port, all_services=all_services)
    with open(file_path, "w") as file:
        file.write(haproxy_config + "\n")
    logging.info("Generated haproxy.cfg")

def build_env_list(provider_key: str, required_env: list, optional_env: list) -> list:
    provider_name = provider_key.replace('_', ' ').lower()
    env_list = [f"VPN_SERVICE_PROVIDER={provider_name}"]
    for env in required_env + optional_env:
        for key, value in env.items():
            env_list.append(f"{key}={value}")
    return env_list

def manage_containers(action: str, config: dict):
    generate_compose_file(config)
    generate_haproxy_config(config)
    try:
        if action == "up":
            try:
                result = subprocess.run(
                    ["docker", "compose", "-f", COMPOSE_FILE, "ps", "-q"],
                    capture_output=True, text=True, check=True
                )
                running_containers = result.stdout.strip()
                if running_containers:
                    logging.info("Containers already running, restarting them.")
                    subprocess.run(["docker", "compose", "-f", COMPOSE_FILE, "down"], check=True)
                subprocess.run(["docker", "compose", "-f", COMPOSE_FILE, "up", "-d"], check=True)
                logging.info("Started or restarted VPN containers and HAProxy")
            except subprocess.CalledProcessError as e:
                logging.error(f"Failed to start containers: {e}")
                sys.exit(1)
        elif action == "down":
            try:
                subprocess.run(["docker", "compose", "-f", COMPOSE_FILE, "down"], check=True)
                logging.info("Stopped VPN containers and HAProxy")
            except subprocess.CalledProcessError as e:
                logging.error(f"Failed to stop containers: {e}")
                sys.exit(1)
    except Exception as e:
        logging.error(f"An error occurred: {e}")
        sys.exit(1)

def list_containers():
    try:
        result = subprocess.run(
            ["docker", "compose", "-f", COMPOSE_FILE, "ps", "--services"],
            capture_output=True, text=True, check=True
        )
        services = [name.strip() for name in result.stdout.split("\n") if name.strip()]
        logging.info(f"Services: {', '.join(services)}")
        return services
    except subprocess.CalledProcessError as e:
        logging.error(f"Failed to list containers: {e}")
        sys.exit(1)

def run_command_through_proxy(cmd: list, config: dict):
    proxy_port = config["global_settings"]["proxy_port"]
    full_cmd = [
        "docker", "compose", "-f", COMPOSE_FILE, "exec",
        "-e", f"ALL_PROXY=socks5h://haproxy:{proxy_port}",
        "haproxy"
    ] + cmd
    try:
        subprocess.run(full_cmd, check=True)
        logging.info(f"Ran command through HAProxy: {' '.join(cmd)}")
    except subprocess.CalledProcessError as e:
        logging.error(f"Failed to run command through proxy: {e}")
        sys.exit(1)

def start_interactive_shell(config: dict):
    proxy_port = config["global_settings"]["proxy_port"]
    cmd = [
        "docker", "compose", "-f", COMPOSE_FILE, "exec",
        "-e", f"ALL_PROXY=socks5h://haproxy:{proxy_port}",
        "haproxy", "sh"
    ]
    logging.info(f"Starting interactive shell through HAProxy on port {proxy_port}")
    try:
        subprocess.run(cmd)
    except subprocess.CalledProcessError as e:
        logging.error(f"Failed to start interactive shell: {e}")
        sys.exit(1)

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
    if not os.path.exists(ENV_FILE):
        logging.error(
            f"The {ENV_FILE} file is missing. Please create it with the required environment variables."
        )
        sys.exit(1)
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
