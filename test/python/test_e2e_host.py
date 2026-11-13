import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("plugin_e2e", ROOT / "test" / "plugin_e2e.py")
e2e = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(e2e)

ENGINE = {"Name": "ci-runner", "OperatingSystem": "Ubuntu 24.04 LTS",
          "SecurityOptions": ["name=apparmor", "name=seccomp,profile=builtin"]}
BRIDGE = [{"IPAM": {"Config": [{"Subnet": "fd00::/64", "Gateway": "fd00::1"},
                               {"Subnet": "172.17.0.0/16", "Gateway": "172.17.0.1"}]}}]


def choose(system="Linux", override=None, info=ENGINE, bridge=BRIDGE):
    return e2e.choose_provider_host(system, override, info, bridge)


class ProviderHostTests(unittest.TestCase):
    def test_override_wins(self):
        self.assertEqual(choose(override="10.1.2.3", info=None, bridge=None), "10.1.2.3")

    def test_invalid_override_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "E2E_PROVIDER_HOST"):
            choose(override="not-an-address")

    def test_macos_uses_loopback(self):
        self.assertEqual(choose(system="Darwin", info=None, bridge=None), "127.0.0.1")

    def test_engines_that_forward_to_host_loopback_use_loopback(self):
        for info in [{**ENGINE, "OperatingSystem": "Docker Desktop"},
                     {**ENGINE, "Name": "colima"},
                     {**ENGINE, "Name": "lima-default"}]:
            self.assertEqual(choose(info=info, bridge=None), "127.0.0.1", info)

    def test_rootless_docker_requires_the_override(self):
        # rootlesskit defaults to --disable-host-loopback, so neither the host's
        # loopback nor the bridge gateway reliably reaches a host socket.
        rootless = {**ENGINE, "SecurityOptions": ["name=seccomp", "name=rootless"]}
        with self.assertRaisesRegex(RuntimeError, "Rootless Docker.*E2E_PROVIDER_HOST"):
            choose(info=rootless)
        self.assertEqual(choose(override="10.1.2.3", info=rootless), "10.1.2.3")

    def test_linux_uses_first_ipv4_bridge_gateway(self):
        self.assertEqual(choose(), "172.17.0.1")

    def test_linux_without_ipv4_bridge_gateway_names_the_override(self):
        bridge = [{"IPAM": {"Config": [{"Gateway": "fd00::1"}, {"Subnet": "10.0.0.0/8"}]}}]
        with self.assertRaisesRegex(RuntimeError, "E2E_PROVIDER_HOST"):
            choose(bridge=bridge)
        with self.assertRaisesRegex(RuntimeError, "E2E_PROVIDER_HOST"):
            choose(bridge=[])


class ProxyRunCommandTests(unittest.TestCase):
    def command(self, system, provider_host):
        return e2e.proxy_run_command("proxy-1", Path("/work/config"), "image:tag",
                                     provider_host, system)

    def test_common_arguments_publish_on_loopback_only(self):
        command = self.command("Darwin", "127.0.0.1")
        self.assertEqual(command[:6], ["docker", "run", "-d", "--rm", "--name", "proxy-1"])
        self.assertIn("127.0.0.1::4000", command)
        self.assertIn("/work/config:/config:ro", command)
        self.assertEqual(command[-1], "image:tag")

    def test_macos_default_relies_on_the_engine_host_alias(self):
        self.assertNotIn("--add-host", self.command("Darwin", "127.0.0.1"))

    def test_linux_loopback_engine_maps_the_host_gateway(self):
        command = self.command("Linux", "127.0.0.1")
        self.assertEqual(command[command.index("--add-host") + 1],
                         "host.docker.internal:host-gateway")

    def test_linux_bridge_maps_the_alias_to_the_bridge_gateway(self):
        command = self.command("Linux", "172.17.0.1")
        self.assertEqual(command.count("--add-host"), 1)
        self.assertEqual(command[command.index("--add-host") + 1],
                         "host.docker.internal:172.17.0.1")

    def test_explicit_override_maps_the_alias_on_every_platform(self):
        for system in ["Darwin", "Linux"]:
            command = self.command(system, "10.1.2.3")
            self.assertEqual(command.count("--add-host"), 1, system)
            self.assertEqual(command[command.index("--add-host") + 1],
                             "host.docker.internal:10.1.2.3", system)


class GatewayImageTests(unittest.TestCase):
    def test_the_default_and_common_references_are_accepted(self):
        self.assertEqual(e2e.gateway_image({}), "switchyard-litellm:local")
        for image in ["switchyard-litellm:ci", "switchyard-litellm:review", "ghcr.io/berriai/litellm:v1.102.2",
                      "localhost:5000/team/gateway:1.0",
                      "public.ecr.aws/docker/library/rust:1.99.1-slim-bookworm",
                      "gateway@sha256:" + "0" * 64, "ghcr.io/berriai/litellm:v1.102.2@sha256:" + "a" * 64]:
            self.assertEqual(e2e.gateway_image({"GATEWAY_IMAGE": image}), image)

    def test_options_whitespace_and_shell_characters_are_rejected(self):
        for image in ["", "-v", "--privileged", "image tag", "image:tag ", "image:tag\n", "image;id",
                      "image$(id)", "image`id`", "image|cat", "image&", "Image:Tag", "../image"]:
            with self.assertRaisesRegex(RuntimeError, "GATEWAY_IMAGE", msg=repr(image)):
                e2e.gateway_image({"GATEWAY_IMAGE": image})


class ReadyTimeoutTests(unittest.TestCase):
    def test_default_is_180_seconds(self):
        self.assertEqual(e2e.ready_timeout({}), 180.0)
        self.assertEqual(e2e.ready_timeout({"E2E_READY_TIMEOUT": ""}), 180.0)

    def test_override_is_read_in_seconds(self):
        self.assertEqual(e2e.ready_timeout({"E2E_READY_TIMEOUT": "300"}), 300.0)
        self.assertEqual(e2e.ready_timeout({"E2E_READY_TIMEOUT": "2.5"}), 2.5)

    def test_invalid_override_is_rejected(self):
        for value in ["soon", "0", "-5", "nan", "inf"]:
            with self.assertRaisesRegex(RuntimeError, "E2E_READY_TIMEOUT"):
                e2e.ready_timeout({"E2E_READY_TIMEOUT": value})


if __name__ == "__main__":
    unittest.main()
