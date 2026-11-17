import os
import json
import tempfile


STRICT_TYPES = [
    # "main_frame",
    "sub_frame",
    "stylesheet",
    "script",
    "image",
    "font",
    "object",
    "xmlhttprequest",
    "ping",
    "csp_report",
    "media",
    "websocket",
    "other",
]

VALID_TYPES = {"main_frame", "sub_frame", "stylesheet", "script", "image", "font",
               "object", "xmlhttprequest", "ping", "csp_report", "media",
               "websocket", "webtransport", "other"}


WPT_URLS = ["*://*.not-web-platform.test/*", "*://*.web-platform.test/*"]


class ExtensionSettings(object):
    def __init__(
        self,
        ext,
        ext_dir: str,
        resource_types: list[str] = STRICT_TYPES,
        urls: list[str] = WPT_URLS,
    ):
        self.main_rule = [
            {
                "id": 19,
                "priority": 2000,
                "urlFilter": urls,
                "action": {"type": "block"},
                "condition": {"resourceTypes": STRICT_TYPES, "urlFilter": urls},
            }
        ]
        self.ext_dir = os.path.abspath(ext_dir)
        self.ext = ext
        print("Extension = ", self.ext)
        self.resource_types = resource_types
        self.urls = urls
        if self.resource_types != STRICT_TYPES:
            self.main_rule[0]["condition"]["resourceTypes"] = self.resource_types
        if not os.path.isfile(os.path.join(self.ext_dir, "manifest.json")):
            raise RuntimeError(
                f"{self.ext_dir} has no manifest.json; wrong extension path?"
            )
        self.rule_file = None
        self.set_rule_file()
        self.ext = ext

    def set_rule_file(self) -> str:
        if self.ext == "Ublock_Lite":
            self.rule_file = "rulesets/main/ubol-tests.json"
        elif self.ext == "Ublock_origin":
            self.rule_file = "rulesets/main/ubol-tests.json"
        elif self.ext == "Adblock_plus":
            self.rule_file = "data/rules/dnr/TestRuleset"
        elif self.ext == "Adblock":
            self.rule_file = "data/rules/dnr/TestRuleset"
        elif self.ext == "Adguard":
            self.rule_file = "filters/declarative/ruleset_261/test_rules.json"
        elif self.ext == "Ghostery":
            self.rule_file = "rule_resources/ghostery-tests.json"
        else:
            raise RuntimeError(f"Unknown extension {self.ext}")

    def update_manifest(self, add_cust_rule=True) -> None:
        """Update the extension's manifest to include the rule file."""

        manifest_path = os.path.join(self.ext_dir, "manifest.json")

        with open(manifest_path, "r") as fh:
            manifest = json.load(fh)

        manifest_rule = {
            "id": "test_rule",
            "enabled": True,
            "path": f"/{self.rule_file}",
        }
        if (
            manifest_rule not in manifest["declarative_net_request"]["rule_resources"]
            and add_cust_rule
        ):
            print("Adding rule resource to manifest.json")
            manifest["declarative_net_request"]["rule_resources"].append(manifest_rule)

        elif (
            not add_cust_rule
            and manifest_rule in manifest["declarative_net_request"]["rule_resources"]
        ):
            print("Removing rule resource from manifest.json")
            manifest["declarative_net_request"]["rule_resources"] = [
                r
                for r in manifest["declarative_net_request"]["rule_resources"]
                if r.get("id") != "test_rule"
            ]

        else:
            print("Rule resource already present/removed in/from manifest.json")

        with open(manifest_path, "w") as fh:
            json.dump(manifest, fh, indent=2)

    def update_manifest_v2(self, add_cust_rule=True) -> None:
        manifest_path = os.path.join(self.ext_dir, "manifest.json")
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
        if add_cust_rule:
            perms = manifest.setdefault("permissions", [])
            if "declarativeNetRequest" not in perms:
                perms.append("declarativeNetRequest")

            resources = manifest.setdefault("declarative_net_request", {}).setdefault(
                "rule_resources", []
            )
            resources[:] = [r for r in resources if r.get("id") != "test_rule"]

            print("Adding rule resource to manifest.json")
            resources.append(
                {"id": "test_rule", "enabled": True, "path": self.rule_file}
            )
        else:
            if "declarative_net_request" in manifest:
                print("Removing rule resource from manifest.json")
                del manifest["declarative_net_request"]
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)

    def add_test_rule(self) -> None:
        """Overwrite the extension's TestRuleset with one block rule."""

        rule = self.main_rule.copy()
        bad = set(self.main_rule[0]["condition"]["resourceTypes"]) - VALID_TYPES
        if bad:
            raise RuntimeError(f"invalid resourceTypes in rule: {bad}")
        dst = os.path.join(self.ext_dir, self.rule_file)
        os.makedirs(os.path.dirname(dst), exist_ok=True)

        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dst))
        with os.fdopen(fd, "w") as fh:
            json.dump(rule, fh)
        os.replace(tmp, dst)
