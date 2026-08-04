import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import app as app_module


class AppSchedulerLifecycleTest(unittest.TestCase):
    def _settings(self, *, debug):
        return SimpleNamespace(
            app=SimpleNamespace(host="127.0.0.1", port=5000, debug=debug)
        )

    def test_normal_start_runs_and_stops_k8s_scheduler(self):
        flask_app = MagicMock()
        scheduler_thread = object()
        with patch.object(app_module, "settings", self._settings(debug=False)):
            with patch.object(app_module, "create_app", return_value=flask_app):
                with patch.object(
                    app_module,
                    "start_k8s_background_updater",
                    return_value=scheduler_thread,
                ) as start_scheduler:
                    with patch.object(
                        app_module, "stop_k8s_background_updater"
                    ) as stop_scheduler:
                        with patch.dict(os.environ, {}, clear=True):
                            app_module.run_app()

        start_scheduler.assert_called_once_with()
        flask_app.run.assert_called_once_with(
            host="127.0.0.1", port=5000, debug=False
        )
        stop_scheduler.assert_called_once_with()

    def test_debug_reloader_parent_does_not_start_scheduler(self):
        flask_app = MagicMock()
        with patch.object(app_module, "settings", self._settings(debug=True)):
            with patch.object(app_module, "create_app", return_value=flask_app):
                with patch.object(
                    app_module, "start_k8s_background_updater"
                ) as start_scheduler:
                    with patch.object(
                        app_module, "stop_k8s_background_updater"
                    ) as stop_scheduler:
                        with patch.dict(os.environ, {}, clear=True):
                            app_module.run_app()

        start_scheduler.assert_not_called()
        flask_app.run.assert_called_once_with(
            host="127.0.0.1", port=5000, debug=True
        )
        stop_scheduler.assert_not_called()

    def test_debug_reloader_child_runs_and_stops_scheduler(self):
        flask_app = MagicMock()
        scheduler_thread = object()
        with patch.object(app_module, "settings", self._settings(debug=True)):
            with patch.object(app_module, "create_app", return_value=flask_app):
                with patch.object(
                    app_module,
                    "start_k8s_background_updater",
                    return_value=scheduler_thread,
                ) as start_scheduler:
                    with patch.object(
                        app_module, "stop_k8s_background_updater"
                    ) as stop_scheduler:
                        with patch.dict(
                            os.environ, {"WERKZEUG_RUN_MAIN": "true"}, clear=True
                        ):
                            app_module.run_app()

        start_scheduler.assert_called_once_with()
        flask_app.run.assert_called_once_with(
            host="127.0.0.1", port=5000, debug=True
        )
        stop_scheduler.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
