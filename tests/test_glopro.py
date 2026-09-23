"""Offline adapter checks; no production credentials or portal calls."""
import tempfile
import io
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
import shutil
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode
import zipfile

from operator_app.glopro import (
    GloProConnector, GloProError, _filename, _period, _select_requested,
    _validate_workbook, _PaginationEvidence, _browser_channel, _route_request, _transport_mode,
    _matches_report_download,
)
from operator_app.clients import client_is_excluded


def workbook(path):
    with zipfile.ZipFile(path, "w") as output:
        for name in ("[Content_Types].xml", "xl/workbook.xml", "xl/worksheets/sheet1.xml"):
            output.writestr(name, "<test/>")


class FakePage:
    def __init__(self):
        self.clock = 0.0
        self.state = 0
        self.listeners = {}

    def wait_for_timeout(self, milliseconds):
        self.clock += milliseconds / 1000

    def on(self, event, callback):
        self.listeners[event] = callback

    def remove_listener(self, event, callback):
        self.listeners.pop(event, None)


class Pagination:
    def __init__(self, page, enabled, stuck):
        self.page, self.enabled, self.stuck = page, enabled, stuck

    def count(self):
        return int(self.enabled)

    @property
    def first(self):
        return self

    def click(self):
        if not self.stuck:
            self.page.state += 1
        self.page.emit()


class ListingConnector(GloProConnector):
    def __init__(self, pages, stuck=False):
        super().__init__("test-user", "test-password")
        self.pages, self.stuck = pages, stuck

    def _ensure_clients_list(self, page):
        pass

    def _start_client_listing(self, page):
        def emit():
            current = {item["id"]: item for item in self.pages[page.state]}
            previous = {item["id"]: item for item in self.pages[page.state - 1]} if page.state else {}
            new = [item for ident, item in current.items() if ident not in previous]
            payload = {"success": True, "data": {"more": self.stuck or page.state < len(self.pages) - 1, "items": [{"CLIENT_ID": item["id"], "CLIENT_NAME": item["name"]} for item in new]}}
            response = SimpleNamespace(url="https://lk.glopro.ru/clients/clients-list", request=SimpleNamespace(post_data=f"offset={len(previous)}"), json=lambda: payload)
            page.listeners["response"](response)
        page.emit = emit
        emit()

    def _loading(self, page):
        return False

    def _items(self, page):
        return self.pages[page.state]

    def _more(self, page):
        return Pagination(page, self.stuck or page.state < len(self.pages) - 1, self.stuck)


REPORT_PARAMETERS = {
    "build": "1", "report_id": "19",
    "period_start": "2026-09-18", "period_end": "2026-09-21", "format": "xlsx",
    "client_choose_single": "83706", "contract_choose_single": "112042",
    "contract_field": "contract_choose_single-19_0",
}


def report_query(**overrides):
    parameters = {**REPORT_PARAMETERS, **overrides}
    pairs = [(key, value) for key, value in parameters.items() if key not in {"client_choose_single", "contract_choose_single", "contract_field"}]
    pairs.extend((
        ("additional[0][name]", parameters["contract_field"]),
        ("additional[0][value]", parameters["contract_choose_single"]),
        ("additional[0][weight]", "0"),
        ("additional[0][data][parent][name]", "client_choose_single"),
        ("additional[0][data][parent][value]", parameters["client_choose_single"]),
    ))
    return pairs


def report_url(pairs=None, **overrides):
    return "https://lk.glopro.ru/reports/generate/?" + urlencode(report_query(**overrides) if pairs is None else pairs)


class Download:
    def __init__(self, source, url=None, page=None):
        self.source, self.url, self.page = source, url or report_url(), page

    def failure(self):
        return None

    def save_as(self, path):
        shutil.copyfile(self.source, path)


class DownloadLocator:
    def __init__(self, page, generate=False):
        self.page, self.generate = page, generate

    def evaluate_all(self, script, *args):
        raise AssertionError("Native report downloads must not inspect or click queue history")

    def click(self):
        assert self.generate
        self.page.generate_count += 1
        self.page.emit_due()


class DownloadContext:
    def __init__(self):
        self.listener = None

    def on(self, event, callback):
        assert event == "download"
        self.listener = callback

    def remove_listener(self, event, callback):
        assert event == "download"
        assert callback is self.listener
        self.listener = None


class DownloadPage(FakePage):
    def __init__(self, source=None, links=(), *, delay=0, events=None, popup=False):
        super().__init__()
        self.source, self.links = source, links
        self.context = DownloadContext()
        self.generate_count = 0
        self.clicked_links = []
        self.events = list(events or [])
        self.download_page = SimpleNamespace(name="generated-popup") if popup else self
        if source:
            self.events.append((delay, Download(source, page=self.download_page)))

    def locator(self, selector):
        assert "generateReport" in selector, f"Unexpected queue lookup: {selector}"
        return DownloadLocator(self, "generateReport" in selector)

    def emit_due(self):
        pending = []
        for due, download in self.events:
            if due <= self.clock:
                self.context.listener(download)
            else:
                pending.append((due, download))
        self.events = pending

    def wait_for_timeout(self, milliseconds):
        super().wait_for_timeout(milliseconds)
        self.emit_due()


class GloProTests(unittest.TestCase):
    class Route:
        def __init__(self, url="https://lk.glopro.ru/login", resource="document", fail=False):
            self.request = SimpleNamespace(url=url, resource_type=resource, method="POST")
            self.calls = []
            self.fail = fail
            self.response = SimpleNamespace(dispose=lambda: self.calls.append(("dispose", {})))

        def abort(self, **kwargs):
            self.calls.append(("abort", kwargs))

        def continue_(self):
            self.calls.append(("continue", {}))

        def fetch(self, **kwargs):
            self.calls.append(("fetch", kwargs))
            if self.fail:
                raise RuntimeError("SECRET request password=TEST_PRIVATE_VALUE")
            return self.response

        def fulfill(self, **kwargs):
            self.calls.append(("fulfill", kwargs))

    def test_bridge_keeps_redirects_in_browser_and_never_retries_post(self):
        route = self.Route()
        _route_request(route, "api")
        self.assertEqual([name for name, _ in route.calls], ["fetch", "fulfill", "dispose"])
        self.assertEqual(route.calls[0][1], {"max_redirects": 0, "max_retries": 0, "timeout": 20000})
        self.assertIs(route.calls[1][1]["response"], route.response)

    def test_bridge_is_scoped_to_exact_https_origin(self):
        for url in ["http://lk.glopro.ru/", "https://lk.glopro.ru.evil.example/", "https://another.example/", "https://lk.glopro.ru:8443/", "https://user@lk.glopro.ru/"]:
            route = self.Route(url=url)
            _route_request(route, "api")
            self.assertEqual(route.calls, [("continue", {})])
        route = self.Route()
        _route_request(route, "direct")
        self.assertEqual(route.calls, [("continue", {})])

    def test_blocked_resources_never_fetch(self):
        for resource in ["image", "font", "media"]:
            route = self.Route(resource=resource)
            _route_request(route, "api")
            self.assertEqual(route.calls, [("abort", {})])
        route = self.Route(url="https://mc.yandex.ru/metrika/tag.js", resource="script")
        _route_request(route, "api")
        self.assertEqual(route.calls, [("abort", {})])

    def test_bridge_errors_are_scrubbed_without_automatic_retry(self):
        route = self.Route(fail=True)
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            _route_request(route, "api")
        self.assertEqual(output.getvalue(), "")
        self.assertEqual([name for name, _ in route.calls], ["fetch", "abort"])
        self.assertEqual(route.calls[-1][1], {"error_code": "failed"})

    def test_bridge_disposes_response_if_fulfill_fails(self):
        route = self.Route()
        def fail(**kwargs):
            raise RuntimeError("SECRET transport failure")
        route.fulfill = fail
        _route_request(route, "api")
        self.assertEqual([name for name, _ in route.calls], ["fetch", "abort", "dispose"])

    def test_transport_defaults_to_api_with_explicit_direct_option(self):
        with patch.dict("operator_app.glopro.os.environ", {}, clear=True):
            self.assertEqual(_transport_mode(), "api")
        with patch.dict("operator_app.glopro.os.environ", {"GLOPRO_TRANSPORT": "direct"}):
            self.assertEqual(_transport_mode(), "direct")
        with patch.dict("operator_app.glopro.os.environ", {"GLOPRO_TRANSPORT": "unknown"}):
            with self.assertRaises(GloProError):
                _transport_mode()

    def test_installed_chrome_selected_only_on_mac(self):
        with patch.dict("operator_app.glopro.os.environ", {}, clear=True), patch("operator_app.glopro.Path.is_dir", return_value=True):
            with patch("operator_app.glopro.sys.platform", "darwin"):
                self.assertEqual(_browser_channel(), "chrome")
            with patch("operator_app.glopro.sys.platform", "linux"):
                self.assertIsNone(_browser_channel())
        with patch.dict("operator_app.glopro.os.environ", {}, clear=True), patch("operator_app.glopro.sys.platform", "darwin"), patch("operator_app.glopro.Path.is_dir", return_value=False):
            self.assertIsNone(_browser_channel())

    def test_browser_override_is_validated(self):
        with patch.dict("operator_app.glopro.os.environ", {"GLOPRO_BROWSER_CHANNEL": "chrome"}):
            self.assertEqual(_browser_channel(), "chrome")
        with patch.dict("operator_app.glopro.os.environ", {"GLOPRO_BROWSER_CHANNEL": "chromium"}):
            self.assertIsNone(_browser_channel())
        with patch.dict("operator_app.glopro.os.environ", {"GLOPRO_BROWSER_CHANNEL": "untrusted"}):
            with self.assertRaises(GloProError):
                _browser_channel()

    def test_terminal_response_without_prior_page_is_not_complete(self):
        evidence = _PaginationEvidence()
        evidence.add(20, {"success": True, "data": {"more": False, "items": [{"CLIENT_ID": 21, "CLIENT_NAME": "Б"}]}})
        self.assertIsNone(evidence.complete_ids())

    def test_pagination_requires_terminal_flag_and_contiguous_offsets(self):
        evidence = _PaginationEvidence()
        evidence.add(0, {"success": True, "data": {"more": True, "count": 20, "items": [{"CLIENT_ID": 1, "CLIENT_NAME": "ООО &quot;А&quot;"}]}})
        self.assertIsNone(evidence.complete_ids())
        evidence.add(20, {"success": True, "data": {"more": False, "items": [{"CLIENT_ID": 2, "CLIENT_NAME": "Б"}]}})
        self.assertEqual(evidence.complete_ids(), {"1": 'ООО "А"', "2": "Б"})

    def test_inconsistent_repeat_page_is_rejected(self):
        evidence = _PaginationEvidence()
        evidence.add(0, {"success": True, "data": {"more": False, "items": [{"CLIENT_ID": 1, "CLIENT_NAME": "А"}]}})
        with self.assertRaises(GloProError):
            evidence.add(0, {"success": True, "data": {"more": False, "items": [{"CLIENT_ID": 2, "CLIENT_NAME": "Б"}]}})

    def test_observed_contract_links_cannot_cross_client_or_origin(self):
        class RawLocator:
            def __init__(self, values):
                self.values = values

            def evaluate_all(self, script):
                return self.values

        class Page:
            def __init__(self, contract):
                self.contract = contract

            def locator(self, selector):
                return RawLocator([{"name": "Фирма", "url": "https://lk.glopro.ru/clients/client/100", "contracts": [self.contract]}])

        connector = GloProConnector("x", "y")
        result = connector._items(Page("https://lk.glopro.ru/clients/client/100?contract_id=200"))
        self.assertEqual(result[0]["contracts"][0]["id"], "200")
        for bad in ["https://lk.glopro.ru/clients/client/999?contract_id=200", "https://other.example/clients/client/100?contract_id=200"]:
            with self.assertRaises(GloProError):
                connector._items(Page(bad))

    def test_navigation_retries_are_bounded(self):
        class Page(FakePage):
            url = "https://lk.glopro.ru/"

            def __init__(self):
                super().__init__()
                self.calls = 0

            def goto(self, *args, **kwargs):
                self.calls += 1
                raise TimeoutError("transient navigation failure")

        page = Page()
        with self.assertRaises(GloProError) as failure:
            GloProConnector("x", "y")._goto(page, page.url + "?token=private-diagnostic-value")
        self.assertEqual(page.calls, 3)
        self.assertIn("«/»", str(failure.exception))
        self.assertNotIn("private-diagnostic-value", str(failure.exception))

    def test_dates_require_iso_calendar_and_order(self):
        first, last = _period("2026-09-18", "2026-09-21")
        self.assertEqual((last - first).days, 3)
        for start, end in [("18.09.2026", "2026-09-21"), ("2026-02-30", "2026-03-01"), ("2026-09-22", "2026-09-21")]:
            with self.subTest(start=start), self.assertRaises(GloProError):
                _period(start, end)

    def test_filenames_keep_company_quotes_and_disambiguate_only_collisions(self):
        reserved = set()
        self.assertEqual(_filename('ООО "НАЗВАНИЕ"', "17", reserved), 'ООО "НАЗВАНИЕ".xlsx')
        self.assertEqual(_filename('ООО "НАЗВАНИЕ"', "23", reserved), 'ООО "НАЗВАНИЕ" (23).xlsx')
        self.assertNotIn("/", _filename("../Один/Два\\Три", "99", reserved))
        self.assertNotIn("\\", _filename("../Один/Два\\Три", "99", reserved))

    def test_existing_filenames_are_not_overwritten(self):
        reserved = {'ООО "НАЗВАНИЕ".xlsx'.casefold(), 'ООО "НАЗВАНИЕ" (23).xlsx'.casefold()}
        self.assertEqual(_filename('ООО "НАЗВАНИЕ"', "23", reserved), 'ООО "НАЗВАНИЕ" (23-2).xlsx')

    def test_long_cyrillic_filename_fits_filesystem_byte_limit(self):
        reserved = set()
        first = _filename("Очень длинное название " * 30, "123", reserved)
        second = _filename("Очень длинное название " * 30, "123", reserved)
        self.assertLessEqual(len(first.encode("utf-8")), 255)
        self.assertLessEqual(len(second.encode("utf-8")), 255)

    def test_client_id_resolves_duplicate_names(self):
        clients = [{"id": "1", "name": "ООО А"}, {"id": "2", "name": "ООО А"}]
        with self.assertRaises(GloProError):
            _select_requested(clients, ["ООО А"])
        self.assertEqual(_select_requested(clients, [{"client_id": "2", "client": "ООО А"}]), [clients[1]])
        self.assertEqual(_select_requested(clients, []), clients)
        with self.assertRaises(GloProError):
            _select_requested(clients, [{"id": "2", "name": "Другая фирма"}])

    def test_exclusions_use_id_even_after_rename_and_never_match_another_id(self):
        exclusions = [{"id": "78749", "name": "Китай"}]
        self.assertTrue(client_is_excluded({"id": "78749", "name": "Новое имя"}, exclusions))
        self.assertTrue(client_is_excluded({"client_id": 78749, "client": "Новое имя"}, exclusions))
        self.assertFalse(client_is_excluded({"id": "1", "name": "Китай"}, exclusions))
        self.assertFalse(client_is_excluded({"client_id": "1", "client": "Китай"}, exclusions))

    def test_exclusions_without_source_ids_match_only_whole_normalized_name(self):
        exclusions = [{"id": "78756", "name": 'ООО "НК АРТЭЛЬ"'}, {"id": "78749", "name": "Китай"}]
        self.assertTrue(client_is_excluded({"client": "  ооо «НК АРТЭЛЬ»  "}, exclusions))
        self.assertTrue(client_is_excluded({"name": "КИТАЙ"}, exclusions))
        for name in ("Китай Сервис", 'ИП "НК АРТЭЛЬ"', "НК АРТЭЛЬ", "", "Кита"):
            self.assertFalse(client_is_excluded({"client": name}, exclusions))
        self.assertFalse(client_is_excluded({}, exclusions))

    def download_clients(self, available, folder, exclusions, events, *, clients=None):
        connector = GloProConnector("x", "y")
        with patch.object(connector, "_browser_page", return_value=nullcontext(FakePage())), \
                patch.object(connector, "_login"), \
                patch.object(connector, "_list_clients", return_value=available) as listing, \
                patch.object(connector, "_contracts", return_value=[{"id": "10"}]) as contracts, \
                patch.object(connector, "_prepare_report", return_value={"checked": True}), \
                patch.object(connector, "_download", side_effect=lambda page, path: (workbook(path), _validate_workbook(path))[1]):
            result = connector.download_reports("2026-09-18", "2026-09-21", Path(folder), clients, events.append, exclusions)
        listing.assert_called_once()
        return result, [call.args[1]["id"] for call in contracts.call_args_list]

    def test_download_exclusions_skip_before_contract_requests_and_preserve_others(self):
        available = [{"id": "78749", "name": "Переименованный Китай"}, {"id": "78756", "name": 'ООО "НК АРТЭЛЬ"'},
                     {"id": "1", "name": "Китай"}, {"id": "2", "name": "Китай Сервис"}]
        exclusions = [{"id": "78749", "name": "Китай"}, {"id": "78756", "name": 'ООО "НК АРТЭЛЬ"'}]
        events = []
        with tempfile.TemporaryDirectory() as folder:
            result, contract_clients = self.download_clients(available, folder, exclusions, events)
            self.assertEqual(len(list(Path(folder).glob("*.xlsx"))), 2)
        self.assertEqual([item["client_id"] for item in result], ["1", "2"])
        self.assertEqual(contract_clients, ["1", "2"])
        skipped = [event for event in events if event["stage"] == "skipped"]
        self.assertEqual([event["client_id"] for event in skipped], ["78749", "78756"])
        self.assertTrue(all(event["client"] and event["reason"] == "excluded_client" for event in skipped))
        self.assertEqual([event["total"] for event in events if event["stage"] == "clients"], [2])

    def test_unmatched_exclusions_keep_complete_selected_result(self):
        available = [{"id": "1", "name": "А"}, {"id": "2", "name": "Б"}]
        with tempfile.TemporaryDirectory() as folder:
            result, contract_clients = self.download_clients(available, folder, [{"id": "78749", "name": "Китай"}], [])
        self.assertEqual([item["client_id"] for item in result], ["1", "2"])
        self.assertEqual(contract_clients, ["1", "2"])

    def test_all_selected_excluded_is_not_success(self):
        exclusions = [{"id": "78749", "name": "Китай"}]
        available = exclusions + [{"id": "1", "name": "А"}]
        events = []
        with tempfile.TemporaryDirectory() as folder, self.assertRaisesRegex(GloProError, "нет клиентов для отчёта"):
            self.download_clients(available, folder, exclusions, events, clients=["Китай"])
        self.assertNotIn("downloaded", [event["stage"] for event in events])

    def test_exclusions_cannot_bypass_client_list_completeness(self):
        connector = GloProConnector("x", "y")
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(connector, "_browser_page", return_value=nullcontext(FakePage())), \
                patch.object(connector, "_login"), \
                patch.object(connector, "_list_clients", side_effect=GloProError("Частичная выгрузка")), \
                patch.object(connector, "_contracts") as contracts:
            with self.assertRaisesRegex(GloProError, "Частичная выгрузка"):
                connector.download_reports("2026-09-18", "2026-09-21", Path(folder), excluded_clients=[{"id": "78749", "name": "Китай"}])
            contracts.assert_not_called()

    def test_enumerates_all_pages_and_deduplicates_ui_items(self):
        one = {"id": "1", "name": "ООО А"}
        two = {"id": "2", "name": "ООО Б"}
        three = {"id": "3", "name": "ООО В"}
        connector = ListingConnector([[one, one], [one, one, two, two], [one, two, three]])
        page = FakePage()
        with patch("operator_app.glopro.time.monotonic", side_effect=lambda: page.clock):
            self.assertEqual(connector._list_clients(page), [one, two, three])

    def test_stuck_pagination_never_returns_partial_success(self):
        connector = ListingConnector([[{"id": "1", "name": "ООО А"}]], stuck=True)
        page = FakePage()
        with patch("operator_app.glopro.time.monotonic", side_effect=lambda: page.clock):
            with self.assertRaisesRegex(GloProError, "Частичная выгрузка"):
                connector._list_clients(page)

    def test_hidden_load_controls_do_not_mean_end_of_list(self):
        class HiddenMore(ListingConnector):
            def _more(self, page):
                return Pagination(page, False, True)

        connector = HiddenMore([[{"id": "1", "name": "А"}]], stuck=True)
        page = FakePage()
        with patch("operator_app.glopro.time.monotonic", side_effect=lambda: page.clock):
            with self.assertRaisesRegex(GloProError, "Частичная выгрузка"):
                connector._list_clients(page)

    def test_terminal_response_cannot_hide_missing_dom_firm(self):
        class PartialDOM(ListingConnector):
            def _items(self, page):
                return self.pages[page.state][:1]

        connector = PartialDOM([[{"id": "1", "name": "А"}, {"id": "2", "name": "Б"}]])
        page = FakePage()
        with patch("operator_app.glopro.time.monotonic", side_effect=lambda: page.clock):
            with self.assertRaisesRegex(GloProError, "Частичная выгрузка"):
                connector._list_clients(page)

    def test_account_check_reads_actual_preview_dates(self):
        class Panel:
            def wait_for(self, **kwargs):
                pass

            def inner_text(self):
                return "Последние транзакции\n15.09.2026 12:30\n18.09.2026 09:00"

            def locator(self, selector):
                return SimpleNamespace(count=lambda: 0)

        class Page(FakePage):
            def locator(self, selector):
                return Panel()

        result = GloProConnector("x", "y")._read_account_preview(Page())
        self.assertTrue(result["checked"])
        self.assertEqual(result["visible_dates"], ["2026-09-15", "2026-09-18"])
        self.assertEqual(result["visible_date_count"], 2)
        self.assertEqual(result["scope"], "visible_account_preview_only")

    def test_invalid_client_identifiers_fail_closed(self):
        with self.assertRaises(GloProError):
            GloProConnector._merge_items({}, [{"id": "", "name": "Фирма"}])
        with self.assertRaises(GloProError):
            GloProConnector._merge_items({"1": {"id": "1", "name": "А"}}, [{"id": "1", "name": "Б"}])

    def download_report(self, page, destination, *, timeout=120):
        with patch.object(GloProConnector, "_download_parameters", return_value=dict(REPORT_PARAMETERS)), patch("operator_app.glopro.time.monotonic", side_effect=lambda: page.clock):
            return GloProConnector("x", "y", timeout=timeout)._download(page, destination)

    def test_download_url_matches_full_report_scope_independent_of_query_order(self):
        pairs = [(key.replace("additional[0]", "additional[7]"), value) for key, value in reversed(report_query())]
        self.assertTrue(_matches_report_download(report_url(pairs), REPORT_PARAMETERS))

    def test_download_url_rejects_missing_duplicate_or_changed_scope(self):
        for field in ("build", "report_id", "period_start", "period_end", "format"):
            for mode in ("missing", "duplicate", "changed", "empty"):
                with self.subTest(field=field, mode=mode):
                    pairs = report_query()
                    if mode == "missing":
                        pairs = [(key, value) for key, value in pairs if key != field]
                    elif mode == "duplicate":
                        pairs.append((field, REPORT_PARAMETERS[field]))
                    else:
                        pairs = [(key, ("wrong" if mode == "changed" else "") if key == field else value) for key, value in pairs]
                    self.assertFalse(_matches_report_download(report_url(pairs), REPORT_PARAMETERS))
        for key in ("additional[0][name]", "additional[0][value]", "additional[0][data][parent][name]", "additional[0][data][parent][value]"):
            for mode in ("missing", "duplicate", "changed", "empty"):
                with self.subTest(field=key, mode=mode):
                    pairs = report_query()
                    if mode == "missing":
                        pairs = [(name, value) for name, value in pairs if name != key]
                    elif mode == "duplicate":
                        pairs.append((key, dict(pairs)[key]))
                    else:
                        pairs = [(name, ("wrong" if mode == "changed" else "") if name == key else value) for name, value in pairs]
                    self.assertFalse(_matches_report_download(report_url(pairs), REPORT_PARAMETERS))

    def test_download_url_rejects_extra_root_client_or_contract_scopes(self):
        for field in ("client_choose_single", "contract_choose_single", REPORT_PARAMETERS["contract_field"], "contract_choose_single-20_0"):
            for with_value in (True, False):
                with self.subTest(field=field, with_value=with_value):
                    pairs = report_query() + [("additional[9][name]", field)]
                    if with_value:
                        pairs.append(("additional[9][value]", "999"))
                    self.assertFalse(_matches_report_download(report_url(pairs), REPORT_PARAMETERS))

    def test_download_url_rejects_client_parent_on_a_different_root_scope(self):
        pairs = [(key.replace("additional[0][data]", "additional[9][data]"), value) for key, value in report_query()]
        self.assertFalse(_matches_report_download(report_url(pairs), REPORT_PARAMETERS))

    def test_download_url_rejects_foreign_origin_or_unrelated_report(self):
        query = "?" + urlencode(report_query())
        for base in (
            "http://lk.glopro.ru/reports/generate/",
            "https://lk.glopro.ru.evil.example/reports/generate/",
            "https://lk.glopro.ru@evil.example/reports/generate/",
            "https://user@lk.glopro.ru/reports/generate/",
            "https://lk.glopro.ru:8443/reports/generate/",
            "https://lk.glopro.ru/upload/report/historical.xlsx",
            "https://lk.glopro.ru/reports/generate/another/",
        ):
            with self.subTest(base=base):
                self.assertFalse(_matches_report_download(base + query, REPORT_PARAMETERS))

    def test_download_validates_xlsx_and_publishes_atomically(self):
        with tempfile.TemporaryDirectory() as folder:
            source, destination = Path(folder) / "source.xlsx", Path(folder) / 'ООО "А".xlsx'
            workbook(source)
            page = DownloadPage(source)
            metadata = self.download_report(page, destination)
            self.assertGreater(metadata["bytes"], 0)
            self.assertEqual(len(metadata["sha256"]), 64)
            self.assertEqual(destination.read_bytes(), source.read_bytes())
            self.assertEqual(page.generate_count, 1)
            self.assertIsNone(page.context.listener)
            self.assertFalse(destination.with_name("." + destination.name + ".partial").exists())

    def test_delayed_native_download_ignores_multiple_new_history_links(self):
        class HistoryRefreshPage(DownloadPage):
            def wait_for_timeout(self, milliseconds):
                self.links = ["https://lk.glopro.ru/upload/report/older-a.xlsx", "https://lk.glopro.ru/upload/report/older-b.xlsx"]
                super().wait_for_timeout(milliseconds)

        with tempfile.TemporaryDirectory() as folder:
            source, destination = Path(folder) / "source.xlsx", Path(folder) / "result.xlsx"
            workbook(source)
            page = HistoryRefreshPage(source, delay=0.6)
            self.download_report(page, destination)
            self.assertEqual(destination.read_bytes(), source.read_bytes())
            self.assertEqual(len(page.links), 2)
            self.assertEqual(page.clicked_links, [])
            self.assertEqual(page.generate_count, 1)
            self.assertIsNone(page.context.listener)

    def test_context_catches_native_download_from_popup(self):
        with tempfile.TemporaryDirectory() as folder:
            source, destination = Path(folder) / "source.xlsx", Path(folder) / "result.xlsx"
            workbook(source)
            page = DownloadPage(source, delay=0.2, popup=True)
            self.assertIsNot(page.download_page, page)
            self.download_report(page, destination)
            self.assertTrue(destination.exists())
            self.assertEqual(page.generate_count, 1)
            self.assertEqual(page.listeners, {})
            self.assertIsNone(page.context.listener)

    def test_unrelated_downloads_never_replace_matching_native_report(self):
        with tempfile.TemporaryDirectory() as folder:
            source, destination = Path(folder) / "source.xlsx", Path(folder) / "result.xlsx"
            unrelated = Path(folder) / "unrelated.html"
            workbook(source)
            unrelated.write_text("Wrong company or period", encoding="utf-8")
            events = [(0, Download(unrelated, "https://lk.glopro.ru/upload/report/historical.xlsx")),
                      (0.2, Download(unrelated, report_url(client_choose_single="999"))),
                      (0.4, Download(unrelated, report_url(period_start="2026-01-01")))]
            page = DownloadPage(source, delay=0.6, events=events)
            self.download_report(page, destination)
            self.assertEqual(destination.read_bytes(), source.read_bytes())
            self.assertEqual(page.generate_count, 1)
            self.assertIsNone(page.context.listener)

    def test_html_error_is_not_accepted_as_excel(self):
        with tempfile.TemporaryDirectory() as folder:
            source, destination = Path(folder) / "error.html", Path(folder) / "report.xlsx"
            source.write_text("<html>Login</html>")
            page = DownloadPage(source)
            with self.assertRaises(GloProError):
                self.download_report(page, destination)
            self.assertFalse(destination.exists())
            self.assertEqual(sorted(path.name for path in Path(folder).iterdir()), ["error.html"])
            self.assertIsNone(page.context.listener)

    def test_old_report_links_never_substitute_for_failed_new_download(self):
        page = DownloadPage(links=["https://lk.glopro.ru/upload/report/historical.xlsx"])
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "report.xlsx"
            with self.assertRaises(GloProError):
                self.download_report(page, destination, timeout=1)
            self.assertFalse(destination.exists())
        self.assertEqual(page.generate_count, 1)
        self.assertEqual(page.clicked_links, [])
        self.assertIsNone(page.context.listener)

    def test_unrelated_download_timeout_is_sanitized_and_cleans_listener(self):
        secret_url = report_url(client_choose_single="SECRET_CLIENT") + "&token=PRIVATE_TEST_TOKEN"
        page = DownloadPage(events=[(0, Download(None, secret_url))])
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "report.xlsx"
            with self.assertRaises(GloProError) as caught:
                self.download_report(page, destination, timeout=1)
            self.assertFalse(destination.exists())
        message = str(caught.exception)
        self.assertNotIn("PRIVATE_TEST_TOKEN", message)
        self.assertNotIn("SECRET_CLIENT", message)
        self.assertNotIn("https://", message)
        self.assertEqual(page.generate_count, 1)
        self.assertIsNone(page.context.listener)

    def test_zip_without_workbook_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "fake.xlsx"
            with zipfile.ZipFile(source, "w") as output:
                output.writestr("readme.txt", "not a workbook")
            with self.assertRaises(GloProError):
                _validate_workbook(source)

    def test_credentials_absent_from_repr(self):
        connector = GloProConnector("sensitive-user", "sensitive-password")
        self.assertNotIn("sensitive", repr(connector))

    def test_origin_requires_exact_https_host(self):
        self.assertTrue(GloProConnector._origin_checked("https://lk.glopro.ru/reports"))
        for url in ["http://lk.glopro.ru/reports", "https://lk.glopro.ru.evil.example/reports", "https://lk.glopro.ru@evil.example/reports"]:
            self.assertFalse(GloProConnector._origin_checked(url))


if __name__ == "__main__":
    unittest.main()
