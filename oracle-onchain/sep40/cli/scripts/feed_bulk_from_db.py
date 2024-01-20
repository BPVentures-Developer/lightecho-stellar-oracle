import sqlite3
import logging
import importlib.util
import base64
import json
from datetime import datetime
import sys
import subprocess
import math
from contextlib import contextmanager
from typing import List, Dict, Tuple
from pathlib import Path
from lightecho_stellar_oracle import TESTNET_CONTRACT_XLM, TESTNET_CONTRACT_USD

RESOLUTION = 600

mod_spec = importlib.util.spec_from_file_location(
    "local_settings", Path(__file__).resolve().parent.parent / "local_settings.py"
)
assert mod_spec
local_settings = importlib.util.module_from_spec(mod_spec)
sys.modules["local_settings"] = local_settings
assert mod_spec.loader
mod_spec.loader.exec_module(local_settings)

db_path = getattr(local_settings, "API_DB_PATH", None)
if db_path is None:
    db_path = (
        Path(__file__).parent.parent.parent.parent.resolve() / "api" / "db.sqlite3"
    )
cli_dir = Path(__file__).parent.parent.resolve()

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s %(filename)s:%(lineno)d %(levelname)s] %(message)s",
)
logger = logging.getLogger("feed_bulk_from_db.py")


def run_cli(cmd: str) -> Tuple[bool, str]:
    try:
        return True, subprocess.check_output(
            f"./cli {cmd}", shell=True, text=True, cwd=cli_dir, stderr=subprocess.STDOUT
        )
    except subprocess.CalledProcessError as e:
        return False, e.output


@contextmanager
def cursor_ctx():
    conn = sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    try:
        yield cursor
    except Exception as e:
        conn.rollback()
        raise e
    else:
        conn.commit()
    finally:
        conn.close()


def normalize_timestamp(unnormalized_timestamp, resolution):
    # Calculate the closest future timestamp that preserves the resolution
    normalized_timestamp = math.ceil(unnormalized_timestamp / resolution) * resolution
    return normalized_timestamp


def list_to_base64(data_list):
    json_str = json.dumps(data_list)
    json_bytes = json_str.encode("utf-8")
    return base64.b64encode(json_bytes).decode("utf-8")


def log_result_to_db(cmd, success, output):
    query = """
        INSERT INTO feed_bulk_from_db_logs
        (
            command,
            output,
            success
        )
        VALUES
        (
            :command,
            :output,
            :success
        )
    """
    with cursor_ctx() as cursor:
        cursor.execute(
            query,
            {
                "command": cmd,
                "output": output,
                "success": success,
            },
        )


def add_prices_to_blockchain(prices: List[Dict]):
    xlm_based_prices = []
    usd_based_prices = []
    source_symbols = {}
    for price in prices:
        parsed_price = {
            "source": price["source"],
            "asset_type": "other",
            "asset": price["buy_asset"],
            "price": price["price"],
            "timestamp": price["normalized_timestamp"],
        }
        source_symbols.setdefault(price["source"], []).append(price["symbol"])
        if price["sell_asset"] == "XLM":
            xlm_based_prices.append(parsed_price)
        elif price["sell_asset"] == "USD":
            usd_based_prices.append(parsed_price)
        else:
            raise ValueError(f"Unexpected price sell_asset: {price['sell_asset']}")
    if xlm_based_prices:
        cmd = f"--oracle-contract-id {TESTNET_CONTRACT_XLM} oracle add_prices_base64 {list_to_base64(xlm_based_prices)}"
        logger.info(f"cli.py {cmd}")
        success, output = run_cli(cmd)
        logger.info(output)
        log_result_to_db(cmd, success, output)
    if usd_based_prices:
        cmd = f"--oracle-contract-id {TESTNET_CONTRACT_USD} oracle add_prices_base64 {list_to_base64(usd_based_prices)}"
        logger.info(f"cli.py {cmd}")
        success, output = run_cli(cmd)
        logger.info(output)
        log_result_to_db(cmd, success, output)
    for source, symbols in source_symbols.items():
        mark_symbols_as_added_to_blockchain(source, symbols)


def mark_symbols_as_added_to_blockchain(source, symbols):
    placeholders = ", ".join(["?"] * len(symbols))
    query = f"UPDATE prices SET added_to_blockchain = 1 WHERE source = ? AND symbol IN ({placeholders})"
    with cursor_ctx() as cursor:
        cursor.execute(query, [source] + symbols)


def read_prices_from_db():
    query = """
        SELECT
            id,
            updated_at,
            source,
            symbol,
            price,
            sell_asset,
            buy_asset
        FROM prices
        WHERE status = 'active'
          AND asset_type = 'other'
          AND added_to_blockchain = 0
        ORDER BY updated_at DESC
    """
    with cursor_ctx() as cursor:
        cursor.execute(query)
        prices = []
        symbols = {}
        for result in cursor.fetchall():
            result_dict = dict(result)
            if result_dict["source"] not in symbols:
                symbols[result_dict["source"]] = []
            if result_dict["symbol"] in symbols[result_dict["source"]]:
                continue
            current_timestamp = int(datetime.now().timestamp())
            result_dict["normalized_timestamp"] = normalize_timestamp(
                current_timestamp, RESOLUTION
            )
            if result_dict["normalized_timestamp"] <= current_timestamp:
                prices.append(result_dict)
                symbols[result_dict["source"]].append(result_dict["symbol"])
        if len(prices) == 0:
            logger.info("no new prices to feed into the blockchain contract")
        else:
            add_prices_to_blockchain(prices)


if __name__ == "__main__":
    read_prices_from_db()
