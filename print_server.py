import argparse
import json
import base64
import logging
import os
import socket
import ssl
import sys
import time
import textwrap
import tomllib
import unicodedata
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from logging.handlers import TimedRotatingFileHandler
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import win32event
import win32print
import certifi
from dotenv import load_dotenv
from websocket import (
    WebSocketBadStatusException,
    WebSocketConnectionClosedException,
    WebSocketTimeoutException,
    create_connection,
)


SCRIPT_VERSION = "1.0.4"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
SETTINGS_PATH = os.path.join(BASE_DIR, "settings.toml")
LOG_FILE_PATH = os.path.join(BASE_DIR, "print_server.log")
PRINT_CACHE_PATH = os.path.join(BASE_DIR, "printed_cache.json")
SERVICE_STOP_EVENT = None
TICKET_WIDTH = 40
QTY_COL_WIDTH = 8
DESC_COL_WIDTH = TICKET_WIDTH - QTY_COL_WIDTH


def load_environment() -> None:
    if os.path.exists(ENV_PATH):
        load_dotenv(ENV_PATH)
    else:
        raise FileNotFoundError(f"No se encontro el archivo .env en {ENV_PATH}")


def load_settings() -> Dict[str, Any]:
    if not os.path.exists(SETTINGS_PATH):
        return {}
    with open(SETTINGS_PATH, "rb") as settings_file:
        return tomllib.load(settings_file)


def setup_logging() -> None:
    root_logger = logging.getLogger()
    if root_logger.handlers:
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
            handler.close()

    settings = load_settings()
    backup_count = int(get_config_value(settings, ("service", "log_backup_count"), "LOG_BACKUP_COUNT", 7))
    handler = TimedRotatingFileHandler(
        LOG_FILE_PATH,
        when="midnight",
        interval=1,
        backupCount=backup_count,
        encoding="utf-8",
    )
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)

    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)


def init_service_stop_event(event: Any) -> None:
    global SERVICE_STOP_EVENT
    SERVICE_STOP_EVENT = event


def env_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"La variable de entorno {name} es obligatoria")
    return value


def get_nested_value(payload: Dict[str, Any], path: Tuple[str, ...]) -> Any:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def get_settings_value(settings: Dict[str, Any], path: Tuple[str, ...], default: Any = "") -> Any:
    value = get_nested_value(settings, path)
    if value is not None:
        return value

    return default


def get_config_value(settings: Dict[str, Any], path: Tuple[str, ...], env_name: str, default: Any = "") -> Any:
    env_value = os.getenv(env_name)
    if env_value is not None:
        return env_value.strip()

    return get_settings_value(settings, path, default)


def get_env_value(name: str, default: Any = "") -> Any:
    env_value = os.getenv(name)
    if env_value is not None:
        return env_value.strip()
    return default


def get_required_config_value(settings: Dict[str, Any], path: Tuple[str, ...], env_name: str) -> str:
    value = str(get_config_value(settings, path, env_name, "")).strip()
    if not value:
        dotted_path = ".".join(path)
        raise ValueError(f"La configuracion {dotted_path} o la variable {env_name} es obligatoria")
    return value


def config_bool(settings: Dict[str, Any], path: Tuple[str, ...], env_name: str, default: bool) -> bool:
    value = get_config_value(settings, path, env_name, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no"}


def config_int(settings: Dict[str, Any], path: Tuple[str, ...], env_name: str, default: int) -> int:
    return int(get_config_value(settings, path, env_name, default))


def settings_bool(settings: Dict[str, Any], path: Tuple[str, ...], default: bool) -> bool:
    value = get_settings_value(settings, path, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no"}


def settings_int(settings: Dict[str, Any], path: Tuple[str, ...], default: int) -> int:
    return int(get_settings_value(settings, path, default))


def settings_dict(settings: Dict[str, Any], path: Tuple[str, ...], default: Dict[str, Any]) -> Dict[str, Any]:
    value = get_nested_value(settings, path)
    if isinstance(value, dict):
        return value
    return default


def settings_list(settings: Dict[str, Any], path: Tuple[str, ...], default: List[str]) -> List[str]:
    value = get_nested_value(settings, path)
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return default


def config_dict(settings: Dict[str, Any], path: Tuple[str, ...], env_name: str, default: Dict[str, Any]) -> Dict[str, Any]:
    env_value = os.getenv(env_name, "").strip()
    if env_value:
        return parse_json_env(env_name, default)

    value = get_nested_value(settings, path)
    if isinstance(value, dict):
        return value
    return default


def config_list(settings: Dict[str, Any], path: Tuple[str, ...], env_name: str, default: List[str]) -> List[str]:
    env_value = os.getenv(env_name, "").strip()
    if env_value:
        return [item.strip() for item in env_value.split(",") if item.strip()]

    value = get_nested_value(settings, path)
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]

    return default


def parse_json_env(name: str, default: Any) -> Any:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return default
    try:
        return json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"La variable {name} debe contener JSON valido") from exc


def parse_hex_commands(raw_value: str, env_name: str) -> bytes:
    value = raw_value.strip()
    if not value:
        return b""

    command_parts = [part.strip().replace(" ", "") for part in value.split(",") if part.strip()]
    payload = b""
    for part in command_parts:
        try:
            payload += bytes.fromhex(part)
        except ValueError as exc:
            raise ValueError(f"{env_name} debe contener hexadecimal valido") from exc
    return payload


def normalize_center(value: Optional[str]) -> str:
    return (value or "SIN_CENTRO").strip().upper()


def has_printable_center(value: Optional[str]) -> bool:
    return normalize_center(value) != "SIN_CENTRO"


def resolve_print_center(detail: Dict[str, Any], config: "Config") -> str:
    center = normalize_center(detail.get("ProductionCenter"))
    if has_printable_center(center):
        return center
    if config.default_printer_name:
        return "GENERAL"
    return ""


def list_installed_printers() -> List[str]:
    printer_flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    printers = win32print.EnumPrinters(printer_flags)
    names = sorted({printer[2] for printer in printers if len(printer) > 2 and printer[2]})
    return names


def parse_printer_centers(raw_value: str) -> List[str]:
    return [normalize_center(item) for item in raw_value.split(",") if item.strip()]


def update_env_value(file_path: str, key: str, value: str) -> None:
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"No se encontro el archivo {file_path}")

    with open(file_path, "r", encoding="utf-8") as env_file:
        lines = env_file.readlines()

    replacement = f"{key}={value}\n"
    key_prefix = f"{key}="
    updated = False
    for index, line in enumerate(lines):
        if line.startswith(key_prefix):
            lines[index] = replacement
            updated = True
            break

    if not updated:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] = lines[-1] + "\n"
        lines.append(replacement)

    with open(file_path, "w", encoding="utf-8") as env_file:
        env_file.writelines(lines)


def format_toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(format_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        items = [f"{format_toml_value(str(key))} = {format_toml_value(item)}" for key, item in value.items()]
        return "{ " + ", ".join(items) + " }"
    return json.dumps(str(value), ensure_ascii=False)


def write_settings(settings: Dict[str, Any]) -> None:
    lines: List[str] = []
    for section, values in settings.items():
        if not isinstance(values, dict):
            continue
        if lines:
            lines.append("")
        lines.append(f"[{section}]")
        for key, value in values.items():
            lines.append(f"{key} = {format_toml_value(value)}")

    with open(SETTINGS_PATH, "w", encoding="utf-8") as settings_file:
        settings_file.write("\n".join(lines) + "\n")


def update_settings_printer_map(printer_map: Dict[str, str]) -> None:
    settings = load_settings()
    printers = settings.setdefault("printers", {})
    if not isinstance(printers, dict):
        raise ValueError("La seccion printers de settings.toml debe ser una tabla")
    printers["map"] = printer_map
    write_settings(settings)


def load_print_cache() -> Dict[str, float]:
    if not os.path.exists(PRINT_CACHE_PATH):
        return {}
    try:
        with open(PRINT_CACHE_PATH, "r", encoding="utf-8") as cache_file:
            payload = json.load(cache_file)
        if isinstance(payload, dict):
            return {str(key): float(value) for key, value in payload.items()}
    except (OSError, ValueError, TypeError):
        logging.warning("No se pudo leer printed_cache.json. Se recreara.")
    return {}


def save_print_cache(cache: Dict[str, float]) -> None:
    with open(PRINT_CACHE_PATH, "w", encoding="utf-8") as cache_file:
        json.dump(cache, cache_file, indent=2, sort_keys=True)


def cache_detail_printed(cache: Dict[str, float], detail_id: Any) -> None:
    cache[str(detail_id)] = time.time()


def uncache_detail_printed(cache: Dict[str, float], detail_id: Any) -> None:
    cache.pop(str(detail_id), None)


def is_detail_cached(cache: Dict[str, float], detail_id: Any) -> bool:
    return str(detail_id) in cache


def generate_printer_map(centers: List[str]) -> Dict[str, str]:
    printers = list_installed_printers()
    if not printers:
        raise RuntimeError("No se encontraron impresoras instaladas en Windows")

    default_printer = printers[0]
    return {center: default_printer for center in centers}


def handle_cli(argv: List[str]) -> bool:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--list-printers", action="store_true", help="Lista las impresoras instaladas en Windows")
    parser.add_argument(
        "--sync-printer-map",
        action="store_true",
        help="Actualiza printers.map en settings.toml usando las impresoras instaladas",
    )
    parser.add_argument(
        "--centers",
        default="COCINA,BARRA,PARRILLAS",
        help="Centros separados por coma para generar printers.map",
    )
    args = parser.parse_args(argv)

    if args.list_printers:
        printers = list_installed_printers()
        if not printers:
            print("No se encontraron impresoras instaladas.")
            return True
        print("Impresoras instaladas:")
        for printer_name in printers:
            print(f"- {printer_name}")
        return True

    if args.sync_printer_map:
        centers = parse_printer_centers(args.centers)
        if not centers:
            raise ValueError("Debes indicar al menos un centro en --centers")
        printer_map = generate_printer_map(centers)
        update_settings_printer_map(printer_map)
        print("printers.map actualizado en settings.toml")
        print(json.dumps(printer_map, ensure_ascii=False, separators=(",", ":")))
        return True

    return False


class Config:
    def __init__(self) -> None:
        self.settings = load_settings()
        self.api_base_url = str(get_env_value("API_BASE_URL", "https://api.atic.pe")).strip()
        self.company = str(get_env_value("COMPANY", os.getenv("Company", ""))).strip()
        self.auth_mode = str(get_env_value("API_AUTH_MODE", "basic")).strip().lower()
        self.username = str(get_env_value("API_USERNAME", "")).strip()
        self.password = str(get_env_value("API_PASSWORD", "")).strip()
        self.token = str(get_env_value("API_TOKEN", "")).strip()
        self.token_header = str(get_settings_value(self.settings, ("api", "token_header"), "Authorization")).strip()
        self.token_prefix = str(get_settings_value(self.settings, ("api", "token_prefix"), "Bearer")).strip()
        self.timeout = settings_int(self.settings, ("api", "timeout_seconds"), 30)
        self.verify_tls = settings_bool(self.settings, ("api", "verify_tls"), True)
        self.poll_seconds = settings_int(self.settings, ("service", "poll_seconds"), 10)
        self.idle_sleep_seconds = settings_int(
            self.settings,
            ("service", "idle_sleep_seconds"),
            self.poll_seconds,
        )
        self.error_sleep_seconds = settings_int(self.settings, ("service", "error_sleep_seconds"), 30)
        self.websocket_url_override = str(
            get_env_value("WS_TRANSACTIONS_URL", get_settings_value(self.settings, ("websocket", "url"), ""))
        ).strip()
        self.websocket_company_id = str(
            get_settings_value(self.settings, ("websocket", "company_id"), "")
        ).strip()
        self.websocket_url = self._build_websocket_url()
        self.websocket_verify_tls = settings_bool(self.settings, ("websocket", "verify_tls"), self.verify_tls)
        self.websocket_ping_interval_seconds = settings_int(
            self.settings, ("websocket", "ping_interval_seconds"), 20
        )
        self.websocket_ping_timeout_seconds = settings_int(
            self.settings, ("websocket", "ping_timeout_seconds"), 20
        )
        self.websocket_connect_timeout_seconds = settings_int(
            self.settings, ("websocket", "connect_timeout_seconds"), self.timeout
        )
        self.websocket_reconnect_delay_seconds = settings_int(
            self.settings, ("websocket", "reconnect_delay_seconds"), 5
        )
        self.websocket_log_payloads = settings_bool(
            self.settings, ("websocket", "log_payloads"), False
        )
        self.websocket_payload_log_max_chars = settings_int(
            self.settings, ("websocket", "payload_log_max_chars"), 2000
        )
        self.websocket_event_types = {
            item.strip()
            for item in settings_list(
                self.settings,
                ("websocket", "event_types"),
                ["transaction.created", "transaction.updated"],
            )
            if item.strip()
        }
        self.extra_headers = settings_dict(self.settings, ("api", "headers"), {})
        self.printer_map = {
            normalize_center(key): value
            for key, value in settings_dict(self.settings, ("printers", "map"), {}).items()
            if str(value).strip()
        }
        self.default_printer_name = str(
            get_env_value("DEFAULT_PRINTER_NAME", "")
        ).strip()
        self.print_copies = int(
            get_env_value("PRODUCTION_PRINT_COPIES", 1)
        )
        self.precuenta_printer_name = str(
            get_settings_value(self.settings, ("print", "precuenta_printer_name"), "")
        ).strip()
        self.precuenta_copies = settings_int(self.settings, ("print", "precuenta_copies"), 1)
        self.print_encoding = str(get_settings_value(self.settings, ("print", "encoding"), "cp850"))
        self.print_codepage_command_hex = str(
            get_settings_value(self.settings, ("print", "codepage_command_hex"), "1B7402")
        ).strip()
        self.document_title = str(
            get_settings_value(self.settings, ("print", "document_title"), "TicketProduccion")
        )
        self.cut_lines = settings_int(self.settings, ("print", "feed_lines"), 4)
        self.highlight_command_hex = str(
            get_settings_value(self.settings, ("print", "highlight_command_hex"), "1D2111")
        ).strip()
        self.highlight_reset_command_hex = str(
            get_settings_value(self.settings, ("print", "highlight_reset_command_hex"), "1D2100")
        ).strip()
        self.detail_size_command_hex = str(
            get_settings_value(self.settings, ("print", "detail_size_command_hex"), "")
        ).strip()
        self.detail_size_reset_command_hex = str(
            get_settings_value(self.settings, ("print", "detail_size_reset_command_hex"), "1D2100")
        ).strip()
        self.detail_font_command_hex = str(
            get_settings_value(self.settings, ("print", "detail_font_command_hex"), "")
        ).strip()
        self.detail_font_reset_command_hex = str(
            get_settings_value(self.settings, ("print", "detail_font_reset_command_hex"), "")
        ).strip()
        self.detail_use_highlight_style = settings_bool(
            self.settings, ("print", "detail_use_highlight_style"), False
        )
        self.detail_ticket_width = settings_int(self.settings, ("print", "detail_ticket_width"), 40)
        self.detail_quantity_width = settings_int(
            self.settings, ("print", "detail_quantity_width"), 8
        )
        self.detail_description_indent = settings_int(
            self.settings, ("print", "detail_description_indent"), self.detail_quantity_width
        )
        self.cut_enabled = settings_bool(self.settings, ("print", "cut_enabled"), True)
        self.cut_command_hex = str(
            get_settings_value(self.settings, ("print", "cut_command_hex"), "1D5641")
        )
        self.pre_cut_feed_lines = settings_int(self.settings, ("print", "pre_cut_feed_lines"), 3)
        self.after_job_sleep_ms = settings_int(self.settings, ("print", "after_job_sleep_ms"), 300)
        self.combine_ticket_and_cut = settings_bool(
            self.settings, ("print", "combine_ticket_and_cut"), True
        )
        self.beep_enabled = settings_bool(self.settings, ("print", "beep_enabled"), False)
        self.beep_command_hex = str(get_settings_value(self.settings, ("print", "beep_command_hex"), "")).strip()
        self.beep_position = str(
            get_settings_value(self.settings, ("print", "beep_position"), "before_cut")
        ).strip().lower()
        self.reprint_when_not_confirmed = settings_bool(
            self.settings, ("print", "reprint_when_not_confirmed"), False
        )

        if self.auth_mode == "basic" and (not self.username or not self.password):
            raise ValueError("API_USERNAME y API_PASSWORD son obligatorias en .env para API_AUTH_MODE=basic")
        if self.auth_mode in {"bearer", "token"} and not self.token:
            raise ValueError("API_TOKEN es obligatoria en .env para API_AUTH_MODE=bearer/token")
        if not self.default_printer_name and not self.printer_map:
            raise ValueError(
                "DEFAULT_PRINTER_NAME en .env o printers.map en settings.toml es obligatorio para imprimir"
            )
        if not self.websocket_url:
            raise ValueError("WS_TRANSACTIONS_URL es obligatorio o debe poder derivarse desde API_BASE_URL y COMPANY")

    def resolve_printer_name(self, center: Optional[str] = None) -> str:
        if self.default_printer_name:
            return self.default_printer_name
        if center:
            return self.printer_map.get(normalize_center(center), "")
        printer_names = [name for name in self.printer_map.values() if name]
        return printer_names[0] if printer_names else ""

    def _build_websocket_url(self) -> str:
        if self.websocket_url_override:
            return self.websocket_url_override

        company_id = self.company or self.websocket_company_id
        if not company_id:
            return ""

        parsed_url = urlparse(self.api_base_url)
        if not parsed_url.scheme or not parsed_url.netloc:
            return ""

        websocket_scheme = "wss" if parsed_url.scheme == "https" else "ws"
        return f"{websocket_scheme}://{parsed_url.netloc}/ws/companies/{company_id}/transactions/"

    def build_websocket_headers(self) -> List[str]:
        headers: List[str] = []
        for key, value in {"Accept": "application/json", **self.extra_headers}.items():
            headers.append(f"{key}: {value}")

        if self.auth_mode == "basic":
            credentials = f"{self.username}:{self.password}".encode("utf-8")
            encoded_credentials = base64.b64encode(credentials).decode("ascii")
            headers.append(f"Authorization: Basic {encoded_credentials}")
        elif self.auth_mode in {"bearer", "token"}:
            token_value = self.token
            if self.token_prefix:
                token_value = f"{self.token_prefix} {token_value}"
            headers.append(f"{self.token_header}: {token_value}")

        return headers


class TicketPrinter:
    def __init__(self, config: Config) -> None:
        self.config = config

    def print_order_group(self, order: Dict[str, Any], center: str, details: Iterable[Dict[str, Any]]) -> None:
        printer_name = self.config.resolve_printer_name(center)
        if not printer_name:
            raise ValueError("No hay impresora configurada para imprimir produccion")

        raw_bytes = self._build_document_bytes(order, center, list(details))
        beep_bytes = self._build_beep_bytes()
        cut_bytes = self._build_cut_bytes()
        payload_bytes = self._build_payload_bytes(raw_bytes, beep_bytes, cut_bytes)

        logging.info("Enviando pedido %s al centro %s en impresora '%s'", order.get("id"), center, printer_name)
        self._send_to_printer(printer_name, payload_bytes, order.get("id"), center, self.config.print_copies)

    def print_precuenta(self, order: Dict[str, Any]) -> None:
        printer_name = self._resolve_precuenta_printer_name(order)
        if not printer_name:
            raise ValueError("No hay impresora configurada para imprimir la precuenta")

        raw_bytes = self._build_precuenta_bytes(order)
        beep_bytes = self._build_beep_bytes()
        cut_bytes = self._build_cut_bytes()
        payload_bytes = self._build_payload_bytes(raw_bytes, beep_bytes, cut_bytes)

        logging.info("Enviando precuenta del pedido %s a impresora '%s'", order.get("id"), printer_name)
        self._send_to_printer(printer_name, payload_bytes, order.get("id"), "PRECUENTA", self.config.precuenta_copies)

    def print_transaction(self, transaction: Dict[str, Any], details: Iterable[Dict[str, Any]]) -> None:
        printer_name = self.config.resolve_printer_name()
        if not printer_name:
            raise ValueError("No hay impresora configurada para imprimir transacciones")

        raw_bytes = self._build_transaction_bytes(transaction, list(details))
        beep_bytes = self._build_beep_bytes()
        cut_bytes = self._build_cut_bytes()
        payload_bytes = self._build_payload_bytes(raw_bytes, beep_bytes, cut_bytes)

        logging.info("Enviando transaccion %s a impresora '%s'", transaction.get("id"), printer_name)
        self._send_to_printer(printer_name, payload_bytes, transaction.get("id"), "TRANSACCION", self.config.print_copies)

    def _build_payload_bytes(self, raw_bytes: bytes, beep_bytes: bytes, cut_bytes: bytes) -> bytes:
        if not self.config.combine_ticket_and_cut:
            return raw_bytes

        if self.config.beep_position == "before_ticket":
            return beep_bytes + raw_bytes + cut_bytes
        if self.config.beep_position == "after_ticket":
            return raw_bytes + beep_bytes + cut_bytes
        if self.config.beep_position == "after_cut":
            return raw_bytes + cut_bytes + beep_bytes
        return raw_bytes + beep_bytes + cut_bytes

    def _send_to_printer(
        self, printer_name: str, payload_bytes: bytes, order_id: Any, job_label: str, copies: int
    ) -> None:
        printer_handle = win32print.OpenPrinter(printer_name)
        try:
            for copy_number in range(max(copies, 1)):
                job_id = win32print.StartDocPrinter(printer_handle, 1, (self.config.document_title, None, "RAW"))
                try:
                    win32print.StartPagePrinter(printer_handle)
                    win32print.WritePrinter(printer_handle, payload_bytes)
                    win32print.EndPagePrinter(printer_handle)
                    logging.info(
                        "Trabajo de impresion generado. Pedido=%s Tipo=%s Copia=%s JobId=%s",
                        order_id,
                        job_label,
                        copy_number + 1,
                        job_id,
                    )
                finally:
                    win32print.EndDocPrinter(printer_handle)
                    if self.config.after_job_sleep_ms > 0:
                        time.sleep(self.config.after_job_sleep_ms / 1000)
        finally:
            win32print.ClosePrinter(printer_handle)

    def _build_beep_bytes(self) -> bytes:
        if not self.config.beep_enabled:
            return b""
        if not self.config.beep_command_hex:
            raise ValueError("PRINT_BEEP_COMMAND_HEX es obligatorio cuando PRINT_BEEP_ENABLED=true")
        return parse_hex_commands(self.config.beep_command_hex, "PRINT_BEEP_COMMAND_HEX")

    def _build_cut_bytes(self) -> bytes:
        if not self.config.cut_enabled:
            return b""
        pre_cut_feed = b"\n" * max(self.config.pre_cut_feed_lines, 0)
        return pre_cut_feed + parse_hex_commands(self.config.cut_command_hex, "PRINT_CUT_COMMAND_HEX")

    def _build_document_bytes(self, order: Dict[str, Any], center: str, details: List[Dict[str, Any]]) -> bytes:
        order_id = order.get("id", "")
        order_number = f"{order.get('OrderSerie', '')}-{order.get('OrderNumber', '')}".strip("-")
        customer = order.get("ExternalPerson", "")
        cashier = order.get("Cashier") or order.get("InternalPerson") or ""
        workspace = order.get("WorkSpace") or ""
        space = order.get("Space") or ""
        observations = order.get("Observations") or ""
        created_at = order.get("Updated") or order.get("Hour") or order.get("Created") or ""
        header_lines = [
            "=" * TICKET_WIDTH,
            f"CENTRO DE PRODUCCIÓN: {center}".center(TICKET_WIDTH),
            "=" * TICKET_WIDTH,
            f"NUMERO: {order_number}",
            f"FECHA Y HORA: {self._format_datetime(created_at)}",
        ]
        if cashier:
            header_lines.append(f"ASESOR: {cashier}")
        item_lines = []
        item_lines.append("-" * TICKET_WIDTH)
        item_lines.append(self._format_table_header("CANTIDAD", "DESCRIPCIÓN"))
        item_lines.append("-" * TICKET_WIDTH)
        for detail in details:
            quantity = self._format_quantity(detail.get("Quantity"))
            product = (detail.get("Product") or "").strip()
            item_lines.extend(self._format_print_detail_rows(quantity, product))
            detail_obs = (detail.get("Observations") or "").strip()
            if detail_obs:
                item_lines.extend(self._format_print_detail_rows("", f"Obs: {detail_obs}"))

        footer_lines = []
        if observations:
            footer_lines.append(f"OBSERVACIONES: {observations}"[:120])
        footer_lines.append(f"Sayri V {SCRIPT_VERSION}".center(TICKET_WIDTH))

        lines: List[Any] = list(header_lines)
        lines.append("-" * TICKET_WIDTH)
        if workspace:
            lines.append(self._highlight_line(f"SALÓN: {workspace}"))
        if space:
            lines.append(self._highlight_line(f"ESPACIO: {space}"))
        lines.extend(item_lines)
        lines.append("-" * TICKET_WIDTH)
        lines.extend(footer_lines)
        lines.extend([""] * self.config.cut_lines)
        return self._build_codepage_bytes() + self._encode_mixed_lines(lines)

    def _build_transaction_bytes(self, transaction: Dict[str, Any], details: List[Dict[str, Any]]) -> bytes:
        invoice_title = f"{transaction.get('InvoiceSerie') or ''} - {transaction.get('InvoiceNumber') or ''}".strip()
        if invoice_title == "-":
            invoice_title = str(transaction.get("id") or "")
        company_name = str(transaction.get("CompanyName") or "").strip()
        company_document = str(transaction.get("NumberDocumentCompany") or "").strip()
        company_address = str(transaction.get("CompanyAddress") or "").strip()
        customer = str(transaction.get("ExternalPerson") or "").strip()
        customer_document = str(transaction.get("NumberDocumentExternalPerson") or "").strip()
        created_at = transaction.get("Created") or ""

        lines: List[Any] = [
            "=" * TICKET_WIDTH,
            invoice_title.center(TICKET_WIDTH),
            "=" * TICKET_WIDTH,
        ]
        for value in (company_name, company_document, company_address):
            if value:
                lines.extend(chunk.center(TICKET_WIDTH) for chunk in textwrap.wrap(value, width=TICKET_WIDTH) or [""])
        lines.append("=" * TICKET_WIDTH)
        if customer:
            lines.extend(self._wrap_labeled_text("CLIENTE", customer))
        if customer_document:
            lines.extend(self._wrap_labeled_text("RUC-DNI", customer_document))
        lines.extend(self._wrap_labeled_text("FECHA Y HORA", self._format_datetime(str(created_at or ""))))

        lines.append("-" * TICKET_WIDTH)
        lines.append(self._format_transaction_header())
        lines.append("-" * TICKET_WIDTH)

        for detail in details:
            quantity = self._format_quantity(detail.get("Quantity") or detail.get("Qty") or detail.get("Units"))
            product = (
                detail.get("Product")
                or detail.get("Description")
                or detail.get("Name")
                or detail.get("Item")
                or ""
            )
            location = str(detail.get("Location") or "").strip() or "-"
            lines.extend(self._format_transaction_rows(quantity, str(product).strip(), location))
            detail_obs = (detail.get("Observations") or detail.get("Observation") or "").strip()
            if detail_obs:
                for chunk in textwrap.wrap(f"Obs: {detail_obs}", width=TICKET_WIDTH):
                    lines.append(chunk)

        lines.append("-" * TICKET_WIDTH)
        lines.append(f"Sayri V {SCRIPT_VERSION}".center(TICKET_WIDTH))
        lines.extend([""] * self.config.cut_lines)
        return self._build_codepage_bytes() + self._encode_mixed_lines(lines)

    def _build_precuenta_bytes(self, order: Dict[str, Any]) -> bytes:
        order_number = f"{order.get('OrderSerie', '')}-{order.get('OrderNumber', '')}".strip("-")
        cashier = order.get("Cashier") or order.get("InternalPerson") or ""
        workspace = order.get("WorkSpace") or ""
        space = order.get("Space") or ""
        observations = order.get("Observations") or ""
        created_at = order.get("Hour") or order.get("Created") or ""
        details = self._get_precuenta_details(order)
        total = self._sum_precuenta_total(details)
        seller_name = self._extract_precuenta_seller_name(cashier)
        included_ids = [detail.get("id") for detail in details]
        included_id_set = set(included_ids)
        excluded_ids = [
            detail.get("id")
            for detail in order.get("Details") or []
            if detail.get("id") not in included_id_set
        ]
        logging.info(
            "Precuenta calculada. Pedido=%s Incluidos=%s Excluidos=%s TotalCalculado=%s",
            order.get("id"),
            included_ids,
            excluded_ids,
            self._format_money(total),
        )

        lines: List[Any] = [
            "=" * TICKET_WIDTH,
            "PRE - CUENTA".center(TICKET_WIDTH),
            order_number.center(TICKET_WIDTH),
            "=" * TICKET_WIDTH,
        ]
        if workspace:
            lines.append(f"SALÓN: {workspace}")
        if space:
            lines.append(f"MESA: {space}")
        lines.append(f"FEC. EMISIÓN: {self._format_date(created_at)}")
        if seller_name:
            lines.append(f"VENDEDOR: {seller_name}")
        lines.append(f"HORA: {self._format_time(created_at)}")
        lines.append("=" * TICKET_WIDTH)
        lines.append(self._format_precuenta_header())
        lines.append("=" * TICKET_WIDTH)

        for detail in details:
            quantity = self._format_quantity(detail.get("Quantity")) or "0"
            product = (detail.get("Product") or "").strip()
            price = self._format_money(detail.get("Price"))
            amount = self._format_money(detail.get("Amount"))
            lines.extend(self._format_precuenta_rows(quantity, product, price, amount))

        lines.append("=" * TICKET_WIDTH)
        lines.append(f"TOTAL: {self._format_money(total)}".rjust(TICKET_WIDTH))
        lines.append("OBSERVACIONES:")
        if observations:
            for chunk in textwrap.wrap(str(observations), width=TICKET_WIDTH) or [""]:
                lines.append(chunk)
        lines.append("R. SOCIAL: __________________________")
        lines.append("R. U. C.: ___________________________")
        lines.append("DIRECCIÓN: __________________________")
        lines.append("")
        disclaimer = (
            "Este documento NO ES UN COMPROBANTE DE PAGO VÁLIDO y NO otorga derecho a crédito fiscal, "
            "reclame su comprobante en caja."
        )
        for chunk in textwrap.wrap(disclaimer, width=TICKET_WIDTH, break_long_words=False):
            lines.append(chunk.center(TICKET_WIDTH))
        lines.append(f"Sayri V {SCRIPT_VERSION}".center(TICKET_WIDTH))
        lines.extend([""] * self.config.cut_lines)
        return self._build_codepage_bytes() + self._encode_mixed_lines(lines)

    @staticmethod
    def _get_precuenta_details(order: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [
            detail
            for detail in order.get("Details") or []
            if not TicketPrinter._is_truthy_flag(detail.get("Taken"))
            and not TicketPrinter._is_truthy_flag(detail.get("Invoiced"))
        ]

    @staticmethod
    def _is_truthy_flag(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        if isinstance(value, (int, float)):
            return value != 0
        return str(value).strip().lower() in {"1", "true", "yes", "si", "sí"}

    @classmethod
    def _sum_precuenta_total(cls, details: List[Dict[str, Any]]) -> Decimal:
        total = Decimal("0.00")
        for detail in details:
            try:
                total += Decimal(str(detail.get("Amount") or "0.00"))
            except InvalidOperation:
                logging.warning(
                    "Importe invalido en detalle de precuenta. Detalle=%s Amount=%r",
                    detail.get("id"),
                    detail.get("Amount"),
                )
        return total

    @classmethod
    def _sum_detail_total(cls, details: List[Dict[str, Any]]) -> Decimal:
        total = Decimal("0.00")
        for detail in details:
            raw_amount = (
                detail.get("Total")
                or detail.get("Import")
                or detail.get("LineTotal")
                or detail.get("Amount")
                or "0.00"
            )
            try:
                total += Decimal(str(raw_amount))
            except InvalidOperation:
                logging.warning(
                    "Importe invalido en detalle de transaccion. Detalle=%s Amount=%r",
                    detail.get("id"),
                    raw_amount,
                )
        return total

    @staticmethod
    def _first_value(payload: Dict[str, Any], *keys: str) -> Any:
        for key in keys:
            value = payload.get(key)
            if value not in (None, ""):
                return value
        return ""

    @classmethod
    def _build_document_number(cls, payload: Dict[str, Any]) -> str:
        serie = cls._first_value(payload, "Serie", "Series", "DocumentSerie", "OrderSerie")
        number = cls._first_value(payload, "Number", "DocumentNumber", "OrderNumber", "Correlative")
        document_number = f"{serie}-{number}".strip("-")
        return document_number

    @staticmethod
    def _wrap_labeled_text(label: str, value: Any) -> List[str]:
        text = str(value or "").strip()
        if not text:
            return []
        prefix = f"{label}: "
        wrapped = textwrap.wrap(text, width=max(TICKET_WIDTH - len(prefix), 10)) or [""]
        lines = [f"{prefix}{wrapped[0]}"]
        indent = " " * len(prefix)
        lines.extend(f"{indent}{chunk}" for chunk in wrapped[1:])
        return lines

    @staticmethod
    def _format_transaction_header() -> str:
        return f"{'Cantidad':<8} {'Descripcion':<21} {'Ubic.':<7}"

    def _format_transaction_rows(self, quantity: str, description: str, location: str) -> List[str]:
        wrapped_description = textwrap.wrap(
            str(description or ""),
            width=21,
            break_long_words=True,
            break_on_hyphens=True,
        ) or [""]
        rows: List[str] = []
        for index, chunk in enumerate(wrapped_description):
            qty_cell = str(quantity or "") if index == 0 else ""
            location_cell = str(location or "") if index == 0 else ""
            rows.append(f"{qty_cell:<8} {chunk:<21} {location_cell:<7}")
        return rows

    def _resolve_precuenta_printer_name(self, order: Dict[str, Any]) -> str:
        if self.config.precuenta_printer_name:
            return self.config.precuenta_printer_name
        if self.config.default_printer_name:
            return self.config.default_printer_name

        for detail in order.get("Details") or []:
            center = normalize_center(detail.get("ProductionCenter"))
            printer_name = self.config.resolve_printer_name(center)
            if printer_name:
                return printer_name

        return self.config.resolve_printer_name()

    def _build_codepage_bytes(self) -> bytes:
        if not self.config.print_codepage_command_hex:
            return b""
        return parse_hex_commands(self.config.print_codepage_command_hex, "PRINT_CODEPAGE_COMMAND_HEX")

    def _highlight_line(self, text: str) -> bytes:
        highlight_on = parse_hex_commands(self.config.highlight_command_hex, "PRINT_HIGHLIGHT_COMMAND_HEX")
        bold_on = b"\x1b\x45\x01"
        bold_off = b"\x1b\x45\x00"
        highlight_off = parse_hex_commands(
            self.config.highlight_reset_command_hex, "PRINT_HIGHLIGHT_RESET_COMMAND_HEX"
        )
        encoded_text = self._encode_text(text)
        return highlight_on + bold_on + encoded_text + bold_off + highlight_off

    def _format_print_detail_rows(self, quantity: str, description: str) -> List[Any]:
        if self.config.detail_use_highlight_style:
            return [self._highlight_line(row) for row in self._format_sized_detail_rows(quantity, description)]

        if not self.config.detail_size_command_hex:
            return self._format_detail_rows(quantity, description)

        size_on = parse_hex_commands(self.config.detail_size_command_hex, "PRINT_DETAIL_SIZE_COMMAND_HEX")
        size_off = parse_hex_commands(
            self.config.detail_size_reset_command_hex, "PRINT_DETAIL_SIZE_RESET_COMMAND_HEX"
        )
        font_on = parse_hex_commands(self.config.detail_font_command_hex, "PRINT_DETAIL_FONT_COMMAND_HEX")
        font_off = parse_hex_commands(self.config.detail_font_reset_command_hex, "PRINT_DETAIL_FONT_RESET_COMMAND_HEX")
        detail_rows = self._format_sized_detail_rows(quantity, description)
        return [font_on + size_on + self._encode_text(row) + size_off + font_off for row in detail_rows]

    def _encode_mixed_lines(self, lines: List[Any]) -> bytes:
        encoded_lines: List[bytes] = []
        for line in lines:
            if isinstance(line, bytes):
                encoded_lines.append(line)
            else:
                encoded_lines.append(self._encode_text(str(line)))
        return b"\n".join(encoded_lines)

    def _encode_text(self, text: str) -> bytes:
        normalized_text = unicodedata.normalize("NFC", text)
        return normalized_text.encode(self.config.print_encoding, errors="replace")

    def _format_detail_rows(self, quantity: str, description: str) -> List[str]:
        wrapped_description = textwrap.wrap(description, width=DESC_COL_WIDTH) or [""]
        rows: List[str] = []
        for index, chunk in enumerate(wrapped_description):
            qty_value = quantity if index == 0 else ""
            rows.append(self._format_table_row(qty_value, chunk))
        return rows

    def _format_sized_detail_rows(self, quantity: str, description: str) -> List[str]:
        qty_width = max(1, min(self.config.detail_quantity_width, self.config.detail_ticket_width - 1))
        desc_width = max(1, self.config.detail_ticket_width - qty_width)
        continuation_indent = max(0, min(self.config.detail_description_indent, self.config.detail_ticket_width - 1))
        continuation_width = max(1, self.config.detail_ticket_width - continuation_indent)
        wrapped_description = textwrap.wrap(
            " ".join(str(description or "").split()),
            width=desc_width,
            break_long_words=True,
            break_on_hyphens=False,
        ) or [""]
        rows: List[str] = []
        for index, chunk in enumerate(wrapped_description):
            if index == 0:
                qty_text = str(quantity or "")[:qty_width].center(qty_width)
                desc_text = str(chunk or "")[:desc_width].ljust(desc_width)
                rows.append(f"{qty_text}{desc_text}"[: self.config.detail_ticket_width])
                continue

            rows.append((" " * continuation_indent) + str(chunk or "").strip()[:continuation_width])
        return rows

    @staticmethod
    def _format_table_row(quantity: str, description: str) -> str:
        qty_text = str(quantity or "")[:QTY_COL_WIDTH].center(QTY_COL_WIDTH)
        desc_text = str(description or "")[:DESC_COL_WIDTH].ljust(DESC_COL_WIDTH)
        return f"{qty_text}{desc_text}"

    @staticmethod
    def _format_table_header(quantity: str, description: str) -> str:
        qty_text = str(quantity or "")[:QTY_COL_WIDTH].center(QTY_COL_WIDTH)
        desc_text = str(description or "")[:DESC_COL_WIDTH].center(DESC_COL_WIDTH)
        return f"{qty_text}{desc_text}"

    @staticmethod
    def _format_datetime(value: str) -> str:
        if not value:
            return ""
        try:
            return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return value

    @staticmethod
    def _format_quantity(value: Any) -> str:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return str(value or "")
        if numeric.is_integer():
            return str(int(numeric))
        return f"{numeric:.2f}".rstrip("0").rstrip(".")

    @staticmethod
    def _format_money(value: Any) -> str:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return str(value or "0.00")
        return f"{numeric:.2f}"

    @staticmethod
    def _format_date(value: str) -> str:
        if not value:
            return ""
        try:
            return datetime.fromisoformat(value).strftime("%d/%m/%Y")
        except ValueError:
            return value

    @staticmethod
    def _format_time(value: str) -> str:
        if not value:
            return ""
        try:
            return datetime.fromisoformat(value).strftime("%I:%M:%S %p").lstrip("0")
        except ValueError:
            return value

    @staticmethod
    def _extract_precuenta_seller_name(value: str) -> str:
        raw_value = str(value or "").strip()
        if not raw_value:
            return ""
        if " - " in raw_value:
            raw_value = raw_value.split(" - ", 1)[0]
        return raw_value.strip().upper()

    @staticmethod
    def _format_precuenta_header() -> str:
        return f"{'Cant.':<5} {'Detalle':<16} {'Prec.':>7} {'Total':>7}"

    def _format_precuenta_rows(self, quantity: str, description: str, price: str, total: str) -> List[str]:
        wrapped_description = textwrap.wrap(
            str(description or ""),
            width=16,
            break_long_words=True,
            break_on_hyphens=True,
        ) or [""]
        rows: List[str] = []
        for index, chunk in enumerate(wrapped_description):
            qty_cell = str(quantity or "") if index == 0 else ""
            price_cell = str(price or "") if index == 0 else ""
            total_cell = str(total or "") if index == 0 else ""
            rows.append(f"{qty_cell:<5} {chunk:<16} {price_cell:>7} {total_cell:>7}")
        return rows


def group_pending_details(order: Dict[str, Any], config: Config) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for detail in order.get("Details") or []:
        if bool(detail.get("Printed")):
            continue
        center = resolve_print_center(detail, config)
        if not center:
            continue
        grouped[center].append(detail)
    return grouped


def split_order_details(
    order: Dict[str, Any], print_cache: Dict[str, float], config: Config
) -> Tuple[Dict[str, List[Dict[str, Any]]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    grouped_to_print: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    pending_confirmation: List[Dict[str, Any]] = []
    skipped_invalid_center: List[Dict[str, Any]] = []

    for detail in order.get("Details") or []:
        if bool(detail.get("Printed")):
            continue

        center = resolve_print_center(detail, config)
        if not center:
            logging.info(
                "Omitiendo detalle sin centro de produccion valido. Pedido=%s Detalle=%s Producto='%s' Centro='%s'",
                order.get("id"),
                detail.get("id"),
                detail.get("Product"),
                normalize_center(detail.get("ProductionCenter")),
            )
            skipped_invalid_center.append(detail)
            continue

        if not config.reprint_when_not_confirmed and is_detail_cached(print_cache, detail.get("id")):
            pending_confirmation.append(detail)
            continue

        grouped_to_print[center].append(detail)

    return grouped_to_print, pending_confirmation, skipped_invalid_center


def needs_precuenta_print(order: Dict[str, Any]) -> bool:
    return str(order.get("StatusInvoice") or "").strip().lower() == "precuenta"


def log_order_detail_centers(order: Dict[str, Any], config: Config) -> None:
    details = order.get("Details") or []
    if not details:
        logging.info("Pedido %s no contiene detalles.", order.get("id"))
        return

    summary: Dict[str, int] = defaultdict(int)
    for detail in details:
        center = normalize_center(detail.get("ProductionCenter"))
        summary[center] += 1
        printer_name = config.resolve_printer_name(center)
        logging.info(
            "Pedido=%s Detalle=%s Producto='%s' Centro='%s' Impresora='%s' PrintedApi=%s",
            order.get("id"),
            detail.get("id"),
            detail.get("Product"),
            center,
            printer_name or "SIN_MAPEO",
            detail.get("Printed"),
        )

    logging.info("Resumen de centros del pedido %s: %s", order.get("id"), dict(summary))


def wait_for_next_cycle(seconds: int) -> bool:
    if SERVICE_STOP_EVENT:
        wait_result = win32event.WaitForSingleObject(SERVICE_STOP_EVENT, max(seconds, 1) * 1000)
        return wait_result == win32event.WAIT_OBJECT_0
    time.sleep(max(seconds, 1))
    return False


def process_transaction(printer: TicketPrinter, print_cache: Dict[str, float], config: Config, transaction: Dict[str, Any]) -> int:
    transaction_id = transaction.get("id")
    details_to_print: List[Dict[str, Any]] = []

    for detail in transaction.get("Details") or []:
        detail_id = detail.get("id")
        if not config.reprint_when_not_confirmed and detail_id is not None and is_detail_cached(print_cache, detail_id):
            logging.info(
                "Omitiendo detalle de transaccion ya impreso en cache local. Transaccion=%s Detalle=%s",
                transaction_id,
                detail_id,
            )
            continue
        details_to_print.append(detail)

    if not details_to_print:
        logging.info("Transaccion %s sin detalles pendientes para imprimir.", transaction_id)
        return 0

    logging.info("Procesando transaccion %s con %s detalle(s).", transaction_id, len(details_to_print))
    printer.print_transaction(transaction, details_to_print)
    for detail in details_to_print:
        if detail.get("id") is not None:
            cache_detail_printed(print_cache, detail.get("id"))
    save_print_cache(print_cache)
    return len(details_to_print)


def looks_like_transaction(value: Any) -> bool:
    return isinstance(value, dict) and "id" in value and "Details" in value


def extract_transaction_from_event(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if looks_like_transaction(payload):
        return payload

    candidates: List[Any] = [
        payload.get("transaction"),
        payload.get("Transaction"),
        payload.get("data"),
        payload.get("payload"),
        payload.get("message"),
    ]

    for candidate in candidates:
        if looks_like_transaction(candidate):
            return candidate
        if isinstance(candidate, dict):
            nested_transaction = extract_transaction_from_event(candidate)
            if nested_transaction:
                return nested_transaction

    return None


def extract_event_debug_id(payload: Dict[str, Any]) -> Any:
    for key in ("debug_id", "debugId", "request_id", "requestId", "trace_id", "traceId"):
        value = payload.get(key)
        if value:
            return value
    for key in ("transaction", "Transaction", "data", "payload", "message"):
        value = payload.get(key)
        if isinstance(value, dict):
            nested_value = extract_event_debug_id(value)
            if nested_value:
                return nested_value
    return ""


def compact_payload_for_log(payload: Dict[str, Any], max_chars: int) -> str:
    compact_payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(compact_payload) <= max_chars:
        return compact_payload
    return f"{compact_payload[:max_chars]}...<truncated>"


def listen_for_transaction_events(printer: TicketPrinter, print_cache: Dict[str, float], config: Config) -> None:
    logging.info("Escuchando eventos en %s", config.websocket_url)

    while True:
        websocket = None
        try:
            ssl_options = {"cert_reqs": ssl.CERT_REQUIRED if config.websocket_verify_tls else ssl.CERT_NONE}
            if config.websocket_verify_tls:
                ssl_options["ca_certs"] = certifi.where()
            websocket = create_connection(
                config.websocket_url,
                timeout=config.websocket_connect_timeout_seconds,
                header=config.build_websocket_headers(),
                sslopt=ssl_options,
            )
            websocket.settimeout(1)
            last_ping_at = time.monotonic()
            logging.info("Conexion WebSocket establecida correctamente.")

            while True:
                try:
                    raw_message = websocket.recv()
                except WebSocketTimeoutException:
                    now = time.monotonic()
                    if config.websocket_ping_interval_seconds > 0 and (
                        now - last_ping_at
                    ) >= config.websocket_ping_interval_seconds:
                        websocket.ping()
                        last_ping_at = now
                    if SERVICE_STOP_EVENT and wait_for_next_cycle(1):
                        logging.info("Se recibio senal de parada del servicio.")
                        return
                    continue

                if raw_message is None:
                    raise WebSocketConnectionClosedException("La conexion WebSocket fue cerrada")

                payload = json.loads(raw_message)
                event_type = str(payload.get("type", "")).strip()
                debug_id = extract_event_debug_id(payload)
                if event_type == "connection.accepted":
                    logging.info("Canal suscrito: %s", payload.get("group"))
                    continue

                transaction = extract_transaction_from_event(payload)
                logging.info(
                    "Evento WebSocket recibido. Tipo=%s DebugId=%s Keys=%s Transaccion=%s",
                    event_type or "SIN_TIPO",
                    debug_id or "-",
                    sorted(payload.keys()),
                    transaction.get("id") if transaction else "-",
                )
                if config.websocket_log_payloads:
                    logging.info(
                        "Payload WebSocket Tipo=%s DebugId=%s Body=%s",
                        event_type or "SIN_TIPO",
                        debug_id or "-",
                        compact_payload_for_log(payload, config.websocket_payload_log_max_chars),
                    )
                if event_type not in config.websocket_event_types:
                    logging.info("Evento %s omitido por tipo no configurado.", event_type or "SIN_TIPO")
                    continue

                if not transaction:
                    logging.warning(
                        "Evento %s recibido sin payload de transaccion util. DebugId=%s Keys=%s",
                        event_type,
                        debug_id or "-",
                        sorted(payload.keys()),
                    )
                    continue
                if event_type.startswith("transaction."):
                    printed_count = process_transaction(printer, print_cache, config, transaction)
                    logging.info(
                        "Evento procesado. Tipo=%s DebugId=%s Transaccion=%s Detalles impresos=%s Pendientes locales=%s",
                        event_type,
                        debug_id or "-",
                        transaction.get("id"),
                        printed_count,
                        len(print_cache),
                    )
                    continue
        except WebSocketBadStatusException as exc:
            logging.error(
                "No se pudo abrir el WebSocket en %s. El servidor respondio: %s. "
                "Si es 404, el backend no tiene ese canal publicado; configura [websocket].url "
                "en settings.toml con la ruta correcta o pide al backend habilitar /ws/companies/%s/transactions/.",
                config.websocket_url,
                str(exc).split("-+-+-", 1)[0].strip(),
                config.company or config.websocket_company_id,
            )
        except (OSError, socket.error, TimeoutError, WebSocketConnectionClosedException, ConnectionError) as exc:
            logging.warning("Conexion WebSocket interrumpida: %s", exc)
        except json.JSONDecodeError as exc:
            logging.error("Se recibio un mensaje WebSocket invalido: %s", exc, exc_info=True)
        except Exception as exc:
            logging.error("Error procesando eventos WebSocket: %s", exc, exc_info=True)
        finally:
            if websocket is not None:
                try:
                    websocket.close()
                except Exception:
                    pass

        logging.info(
            "Reintentando conexion WebSocket en %s segundo(s).",
            config.websocket_reconnect_delay_seconds,
        )
        if wait_for_next_cycle(config.websocket_reconnect_delay_seconds):
            logging.info("Se recibio senal de parada del servicio.")
            break


def main() -> None:
    load_environment()
    setup_logging()
    config = Config()
    printer = TicketPrinter(config)
    print_cache = load_print_cache()

    logging.info("ATIC Peru")
    logging.info("Iniciando print_server.py version %s", SCRIPT_VERSION)
    logging.info("Modo de escucha configurado por WebSocket.")
    listen_for_transaction_events(printer, print_cache, config)


if __name__ == "__main__":
    if handle_cli(sys.argv[1:]):
        sys.exit(0)
    main()
