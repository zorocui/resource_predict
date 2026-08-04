import dataclasses
import unittest

from resource_predict.settings import bootstrap_settings


class SettingsBoundaryTest(unittest.TestCase):
    def test_bootstrap_settings_contain_only_startup_fields(self):
        self.assertEqual(
            set(dataclasses.asdict(bootstrap_settings)),
            {
                "static_folder", "template_folder", "out_dir", "log_file",
                "log_level", "log_console", "host", "port", "debug",
            },
        )


if __name__ == "__main__":
    unittest.main()
