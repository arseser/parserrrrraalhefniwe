"""
OLX.bg parser — поиск свежих объявлений для перепродажи.

Главное отличие от старой версии: дата регистрации продавца, город, состояние,
цена и т.д. берутся прямо из ответа API (поле user.created), а не парсятся
отдельным запросом на страницу каждого профиля. Поэтому работает в разы быстрее
и меньше шансов получить бан.
"""
import asyncio
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from curl_cffi.requests import AsyncSession
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE = "https://www.olx.bg"
API = f"{BASE}/api/v1"
PAGE_SIZE = 50          # максимум, который отдаёт API за один запрос
MAX_PAGES = 5           # сколько страниц максимум просматривать за один поиск
CACHE_TTL = 60          # секунд, кэш одинаковых запросов
SORTS = {
    "new": "created_at:desc",
    "price_asc": "filter_float_price:asc",
    "price_desc": "filter_float_price:desc",
}

HERE = Path(__file__).parent
session: Optional[AsyncSession] = None
_cache: dict[Any, tuple[float, Any]] = {}
_categories: dict[int, str] = {}
_ads_count: dict[Any, tuple[float, Optional[int]]] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global session
    # curl_cffi имитирует TLS-отпечаток настоящего Chrome — обычный requests/httpx OLX режет
    session = AsyncSession(
        impersonate="chrome",
        timeout=25,
        headers={"Accept-Language": "bg-BG,bg;q=0.9,en;q=0.8", "Accept": "application/json"},
    )
    yield
    await session.close()


app = FastAPI(title="OLX.bg Parser", lifespan=lifespan)


class OlxError(Exception):
    pass


async def olx_get(path: str, params: Optional[dict] = None) -> dict:
    key = (path, tuple(sorted((params or {}).items())))
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    try:
        r = await session.get(f"{API}{path}", params=params)
    except Exception as e:
        raise OlxError(f"Не удалось подключиться к OLX: {e}")
    if r.status_code != 200:
        raise OlxError(f"OLX вернул {r.status_code}: {r.text[:200]}")
    try:
        data = r.json()
    except Exception:
        raise OlxError("OLX вернул не JSON (возможно, блокировка/капча)")
    _cache[key] = (time.time(), data)
    if len(_cache) > 300:  # простая защита от разрастания
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:100]:
            _cache.pop(k, None)
    return data


def parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def ago(dt: Optional[datetime]) -> str:
    if not dt:
        return ""
    sec = int((datetime.now(timezone.utc) - dt).total_seconds())
    if sec < 90:
        return "только что"
    if sec < 3600:
        return f"{sec // 60} мин. назад"
    if sec < 86400:
        return f"{sec // 3600} ч. назад"
    return f"{sec // 86400} дн. назад"


def get_param(item: dict, key: str) -> Optional[dict]:
    for p in item.get("params") or []:
        if p.get("key") == key:
            return p
    return None


async def category_name(cat_id: Optional[int]) -> str:
    if not cat_id:
        return "—"
    if cat_id in _categories:
        return _categories[cat_id]
    try:
        data = await olx_get(f"/categories/{cat_id}")
        name = (data.get("data") or {}).get("name") or "—"
    except OlxError:
        name = "—"
    _categories[cat_id] = name
    return name


async def seller_ads_count(user_id: Any, sem: asyncio.Semaphore) -> Optional[int]:
    hit = _ads_count.get(user_id)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    async with sem:
        try:
            data = await olx_get("/offers/", {"user_id": user_id, "limit": 1})
            total = (data.get("metadata") or {}).get("total_elements")
        except OlxError:
            total = None
    _ads_count[user_id] = (time.time(), total)
    return total


def normalize(item: dict) -> dict:
    user = item.get("user") or {}
    created = parse_dt(item.get("created_time") or item.get("last_refresh_time"))
    reg = parse_dt(user.get("created"))

    price_p = get_param(item, "price") or {}
    pv = price_p.get("value") or {}
    state_p = get_param(item, "state") or {}

    loc = item.get("location") or {}
    city = (loc.get("city") or {}).get("name") or ""
    region = (loc.get("region") or {}).get("name") or ""

    photo = ""
    photos = item.get("photos") or []
    if photos:
        photo = (photos[0].get("link") or "").replace("{width}", "600").replace("{height}", "450")

    delivery = ((item.get("delivery") or {}).get("rock") or {}).get("active")
    seller_id = user.get("uuid") or user.get("id")

    return {
        "id": item.get("id"),
        "title": item.get("title", ""),
        "price": pv.get("label") or "—",
        "price_value": pv.get("value"),
        "negotiable": bool(pv.get("negotiable")),
        "condition": (state_p.get("value") or {}).get("label") or "",
        "city": ", ".join(x for x in (city, region) if x),
        "published": created.astimezone().strftime("%d.%m.%Y %H:%M") if created else "—",
        "published_ago": ago(created),
        "delivery": bool(delivery),
        "is_business": bool(item.get("business")),
        "seller_name": user.get("name") or "—",
        "seller_reg": reg.strftime("%d.%m.%Y") if reg else "—",
        "seller_reg_year": reg.year if reg else None,
        "seller_ads_count": None,
        "ad_url": item.get("url") or f"{BASE}/d/ad/{item.get('id')}.html",
        "seller_url": f"{BASE}/ads/user/{seller_id}/" if seller_id else "",
        "photo_url": photo,
        "category_id": (item.get("category") or {}).get("id"),
        "category": "—",
        "_user_id": user.get("id"),
        "_created": created,
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/search")
async def search(
    q: str = "",
    limit: int = Query(30, ge=1, le=100),
    sort: str = "new",
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    min_seller_year: Optional[int] = None,
    max_seller_year: Optional[int] = None,
    max_listing_age_days: Optional[float] = None,
    only_delivery: bool = False,
    only_private: bool = False,
    with_ads_count: bool = False,
):
    params: dict[str, Any] = {
        "limit": PAGE_SIZE,
        "query": q,
        "sort_by": SORTS.get(sort, SORTS["new"]),
    }
    if min_price is not None:
        params["filter_float_price:from"] = min_price
    if max_price is not None:
        params["filter_float_price:to"] = max_price

    now = datetime.now(timezone.utc)
    results: list[dict] = []
    scanned = 0
    error = None

    for page in range(MAX_PAGES):
        try:
            data = await olx_get("/offers/", {**params, "offset": page * PAGE_SIZE})
        except OlxError as e:
            error = str(e)
            break
        raw = data.get("data") or []
        if not raw:
            break

        stop = False
        for item in raw:
            scanned += 1
            ad = normalize(item)

            # объявления идут от новых к старым — как только вышли за лимит свежести, дальше смысла нет
            if max_listing_age_days is not None and ad["_created"] and sort == "new":
                if (now - ad["_created"]).total_seconds() > max_listing_age_days * 86400:
                    stop = True
                    break
            elif max_listing_age_days is not None and ad["_created"]:
                if (now - ad["_created"]).total_seconds() > max_listing_age_days * 86400:
                    continue

            if only_delivery and not ad["delivery"]:
                continue
            if only_private and ad["is_business"]:
                continue
            if min_seller_year or max_seller_year:
                y = ad["seller_reg_year"]
                if y is None:
                    continue
                if min_seller_year and y < min_seller_year:
                    continue
                if max_seller_year and y > max_seller_year:
                    continue

            results.append(ad)
            if len(results) >= limit:
                stop = True
                break
        if stop or len(raw) < PAGE_SIZE:
            break

    # категории (кэшируются) и, по желанию, количество объявлений продавца
    names = await asyncio.gather(*(category_name(a["category_id"]) for a in results))
    for a, n in zip(results, names):
        a["category"] = n

    if with_ads_count and results:
        sem = asyncio.Semaphore(5)
        counts = await asyncio.gather(*(seller_ads_count(a["_user_id"], sem) for a in results if a["_user_id"]))
        it = iter(counts)
        for a in results:
            if a["_user_id"]:
                a["seller_ads_count"] = next(it)

    for a in results:
        a.pop("_user_id", None)
        a.pop("_created", None)

    return {"items": results, "scanned": scanned, "error": error}


@app.get("/api/raw")
async def raw(q: str = ""):
    """Отладка: сырой первый оффер из API OLX — чтобы посмотреть реальные поля."""
    try:
        data = await olx_get("/offers/", {"limit": 1, "query": q, "sort_by": SORTS["new"]})
    except OlxError as e:
        return {"error": str(e)}
    return (data.get("data") or [None])[0]


app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.get("/")
async def index():
    return FileResponse(HERE / "static" / "index.html")
