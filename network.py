import asyncio
import json
from urllib.parse import urlencode

import urllib.request
import urllib.error


# ====================================================
# ps 通信ランタイム
# HTTP / HTTPS / JSON / Headers / Async
# ====================================================


class NetworkError(Exception):
    """psの通信エラー"""
    pass


# ----------------------------------------------------
# HTTP / HTTPS
# ----------------------------------------------------

def http_request(
    url,
    method="GET",
    data=None,
    headers=None,
    timeout=10
):
    """
    HTTP / HTTPS通信

    url:
        通信先URL

    method:
        GET / POST / PUT / PATCH / DELETE など

    data:
        送信するデータ
        dictならJSONとして送信

    headers:
        HTTPヘッダー

    timeout:
        タイムアウト秒
    """

    if headers is None:
        headers = {}

    body = None

    if data is not None:
        if isinstance(data, dict):
            body = json.dumps(data).encode("utf-8")

            if "Content-Type" not in headers:
                headers["Content-Type"] = "application/json"

        elif isinstance(data, str):
            body = data.encode("utf-8")

        elif isinstance(data, bytes):
            body = data

        else:
            raise NetworkError("送信データの形式が不正です。")

    request = urllib.request.Request(
        url=url,
        data=body,
        headers=headers,
        method=method.upper()
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()

            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("utf-8", errors="replace")

            return {
                "status": response.status,
                "headers": dict(response.headers),
                "text": text,
                "data": parse_json(text)
            }

    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""

        raise NetworkError(
            f"HTTPエラー {e.code}: {body}"
        )

    except urllib.error.URLError as e:
        raise NetworkError(
            f"通信エラー: {e.reason}"
        )

    except TimeoutError:
        raise NetworkError("通信がタイムアウトしました。")

    except Exception as e:
        raise NetworkError(
            f"通信中にエラーが発生しました: {e}"
        )


# ----------------------------------------------------
# GET
# ----------------------------------------------------

def get(url, headers=None, params=None, timeout=10):
    """
    HTTP GET
    """

    if params:
        query = urlencode(params)

        if "?" in url:
            url += "&" + query
        else:
            url += "?" + query

    return http_request(
        url=url,
        method="GET",
        headers=headers,
        timeout=timeout
    )


# ----------------------------------------------------
# POST
# ----------------------------------------------------

def post(url, data=None, headers=None, timeout=10):
    """
    HTTP POST
    """

    return http_request(
        url=url,
        method="POST",
        data=data,
        headers=headers,
        timeout=timeout
    )


# ----------------------------------------------------
# PUT
# ----------------------------------------------------

def put(url, data=None, headers=None, timeout=10):
    """
    HTTP PUT
    """

    return http_request(
        url=url,
        method="PUT",
        data=data,
        headers=headers,
        timeout=timeout
    )


# ----------------------------------------------------
# PATCH
# ----------------------------------------------------

def patch(url, data=None, headers=None, timeout=10):
    """
    HTTP PATCH
    """

    return http_request(
        url=url,
        method="PATCH",
        data=data,
        headers=headers,
        timeout=timeout
    )


# ----------------------------------------------------
# DELETE
# ----------------------------------------------------

def delete(url, headers=None, timeout=10):
    """
    HTTP DELETE
    """

    return http_request(
        url=url,
        method="DELETE",
        headers=headers,
        timeout=timeout
    )


# ----------------------------------------------------
# JSON
# ----------------------------------------------------

def parse_json(text):
    """
    JSON文字列 → Pythonデータ
    """

    try:
        return json.loads(text)

    except (json.JSONDecodeError, TypeError):
        return None


def json_string(data):
    """
    Pythonデータ → JSON文字列
    """

    try:
        return json.dumps(
            data,
            ensure_ascii=False
        )

    except (TypeError, ValueError) as e:
        raise NetworkError(
            f"JSON変換エラー: {e}"
        )


# ----------------------------------------------------
# 非同期HTTP
# ----------------------------------------------------

async def async_get(url, headers=None, params=None, timeout=10):
    """
    非同期GET
    """

    return await asyncio.to_thread(
        get,
        url,
        headers,
        params,
        timeout
    )


async def async_post(url, data=None, headers=None, timeout=10):
    """
    非同期POST
    """

    return await asyncio.to_thread(
        post,
        url,
        data,
        headers,
        timeout
    )


async def async_put(url, data=None, headers=None, timeout=10):
    """
    非同期PUT
    """

    return await asyncio.to_thread(
        put,
        url,
        data,
        headers,
        timeout
    )


async def async_patch(url, data=None, headers=None, timeout=10):
    """
    非同期PATCH
    """

    return await asyncio.to_thread(
        patch,
        url,
        data,
        headers,
        timeout
    )


async def async_delete(url, headers=None, timeout=10):
    """
    非同期DELETE
    """

    return await asyncio.to_thread(
        delete,
        url,
        headers,
        timeout
    )


# ----------------------------------------------------
# ヘルパー
# ----------------------------------------------------

def json_headers(extra=None):
    """
    JSON通信向けの基本ヘッダーを作成
    """

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "ps-runtime/1.0"
    }

    if extra:
        headers.update(extra)

    return headers


def bearer_headers(token, extra=None):
    """
    Bearer認証用ヘッダー
    """

    headers = {
        "Authorization": f"Bearer {token}"
    }

    if extra:
        headers.update(extra)

    return headers
