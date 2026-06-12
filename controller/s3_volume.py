from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import os
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _access_key() -> str:
    return _env("RUNPODS3_ACCESS_KEY_ID") or _env("AWS_ACCESS_KEY_ID")


def _secret_key() -> str:
    return _env("RUNPODS3_SECRET_ACCESS_KEY") or _env("AWS_SECRET_ACCESS_KEY")


def has_s3_credentials() -> bool:
    return bool(_access_key() and _secret_key())


def s3_endpoint(data_center_id: str) -> str:
    return f"https://s3api-{data_center_id.lower()}.runpod.io"


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret_key: str, date_stamp: str, region: str) -> bytes:
    k_date = _sign(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, "s3")
    return _sign(k_service, "aws4_request")


def _canonical_query(params: dict[str, str]) -> str:
    return "&".join(
        urllib.parse.quote(str(key), safe="-_.~") + "=" + urllib.parse.quote(str(value), safe="-_.~")
        for key, value in sorted(params.items())
    )


@dataclass(frozen=True)
class S3Object:
    key: str
    size: int


class RunpodS3VolumeClient:
    def __init__(self, *, data_center_id: str, volume_id: str):
        self.data_center_id = data_center_id
        self.volume_id = volume_id
        self.access_key = _access_key()
        self.secret_key = _secret_key()
        if not self.access_key or not self.secret_key:
            raise RuntimeError("missing_runpod_s3_credentials")

    def _request(
        self,
        method: str,
        *,
        key: str = "",
        payload: bytes = b"",
        query: dict[str, str] | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: int = 120,
    ) -> tuple[int, bytes, dict[str, str]]:
        endpoint = s3_endpoint(self.data_center_id).rstrip("/")
        parsed = urllib.parse.urlparse(endpoint)
        host = parsed.netloc
        path = self.volume_id if not key else f"{self.volume_id}/{key.lstrip('/')}"
        canonical_uri = "/" + urllib.parse.quote(path, safe="/-_.~")
        query_string = _canonical_query(query or {})
        url = endpoint + canonical_uri + (f"?{query_string}" if query_string else "")
        now = dt.datetime.now(dt.UTC)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(payload).hexdigest()
        signed_header_values = {
            "Host": host,
            "X-Amz-Content-Sha256": payload_hash,
            "X-Amz-Date": amz_date,
        }
        if extra_headers:
            signed_header_values.update(extra_headers)
        header_keys = sorted(signed_header_values, key=str.lower)
        canonical_headers = "".join(f"{name.lower()}:{signed_header_values[name].strip()}\n" for name in header_keys)
        signed_headers = ";".join(name.lower() for name in header_keys)
        canonical_request = "\n".join([method, canonical_uri, query_string, canonical_headers, signed_headers, payload_hash])
        scope = f"{date_stamp}/{self.data_center_id}/s3/aws4_request"
        string_to_sign = "\n".join(
            ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()]
        )
        signature = hmac.new(
            _signing_key(self.secret_key, date_stamp, self.data_center_id),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        headers = dict(signed_header_values)
        headers["Authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        headers["Accept"] = "*/*"
        headers["User-Agent"] = "runpod-controller/1.0"
        req = urllib.request.Request(url, data=payload if method != "GET" else None, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    def list_objects(self, prefix: str) -> list[S3Object]:
        # ListObjectsV2 caps each page at 1000 keys; an unpaginated listing would
        # silently drop outputs and let a "successful" collection delete the volume.
        # RunPod's S3 implementation mis-reports IsTruncated=true on final pages
        # and cycles continuation tokens (observed live on EUR-IS-1, 2026-06-11),
        # so termination cannot rely on IsTruncated alone: stop when a page adds
        # no new keys, a token repeats, or the hard page cap is reached.
        objects: dict[str, S3Object] = {}
        continuation_token = ""
        seen_tokens: set[str] = set()
        for _page in range(64):
            query = {"list-type": "2", "prefix": prefix}
            if continuation_token:
                query["continuation-token"] = continuation_token
            status, body, _headers = self._request("GET", query=query, timeout=120)
            if status != 200:
                raise RuntimeError(f"s3_list_failed:{status}:{body[-500:].decode('utf-8', 'replace')}")
            root = ET.fromstring(body)
            new_keys = 0
            for contents in root.findall(".//{*}Contents"):
                key_el = contents.find("{*}Key")
                size_el = contents.find("{*}Size")
                if key_el is not None and key_el.text:
                    if key_el.text not in objects:
                        new_keys += 1
                    objects[key_el.text] = S3Object(key_el.text, int(size_el.text or "0") if size_el is not None else 0)
            truncated = (root.findtext("{*}IsTruncated") or "").strip().lower() == "true"
            continuation_token = (root.findtext("{*}NextContinuationToken") or "").strip()
            if not truncated or not continuation_token or new_keys == 0 or continuation_token in seen_tokens:
                break
            seen_tokens.add(continuation_token)
        return list(objects.values())

    def get_object(self, key: str, *, timeout: int = 180) -> bytes:
        status, body, _headers = self._request("GET", key=key, timeout=timeout)
        if status != 200:
            raise RuntimeError(f"s3_get_failed:{status}:{key}:{body[-500:].decode('utf-8', 'replace')}")
        return body

    def get_text(self, key: str, *, timeout: int = 180) -> str:
        return self.get_object(key, timeout=timeout).decode("utf-8")


def summarize_objects(objects: list[S3Object]) -> list[dict[str, Any]]:
    return [{"key": item.key, "size": item.size} for item in objects]
