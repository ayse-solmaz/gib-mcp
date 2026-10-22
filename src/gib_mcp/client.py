"""gib.gov.tr mevzuat API istemcisi.

Site bir Next.js uygulaması; içerik tarayıcıda https://gib.gov.tr/api/gibportal/...
altındaki belgelenmemiş JSON uç noktalarından yükleniyor. Uç noktalar sitenin
JS paketlerinden çıkarıldı. Kimlik doğrulama gerektirmiyor.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import httpx
from bs4 import BeautifulSoup
from mcp.server.mcpserver.exceptions import ToolError

BASE_URL = "https://gib.gov.tr/api/gibportal/"
DOSYA_URL = "https://cdn.gib.gov.tr/api/gibportal-file/file/getFile?objectKey="

# Sunucu kısa sürede çok istek gelince Retry-After başlığı olmadan 429 döndürüyor.
ISTEK_ARALIGI = 0.5  # saniye, ardışık istekler arası en az süre
BEKLEMELER_429 = (3, 8, 20)  # 429 sonrası sırayla beklenecek süreler

# Site URL'sindeki tür adı -> (API yolu, varsayılan sıralama alanı, yön, açıklama)
TURLER: dict[str, tuple[str, str, str, str]] = {
    "madde": ("madde", "priority", "ASC", "Kanun maddeleri"),
    "gerekce": ("gerekce", "id", "ASC", "Kanun gerekçeleri"),
    "cbk": ("cbk", "resmiGazeteTarih", "DESC", "Cumhurbaşkanı kararları"),
    "bkk": ("bkk", "resmiGazeteTarih", "DESC", "Bakanlar Kurulu kararları"),
    "yonetmelikler": ("yonetmelikler", "resmiGazeteTarih", "DESC", "Yönetmelikler"),
    "teblig": ("teblig", "resmiGazeteTarih", "DESC", "Tebliğler"),
    "icgenelge": ("icGenelge", "id", "DESC", "İç genelgeler"),
    "genelyazilar": ("genelYazilar", "tarih", "DESC", "Genel yazılar"),
    "ozelge": ("ozelge", "ozelgeTarih", "DESC", "Özelgeler"),
    "sirkuler": ("sirkuler", "sirkulerTarih", "DESC", "Sirkülerler"),
}

# Arama uç noktasının beklediği entityType değerleri
ARAMA_TURLERI: dict[str, str] = {
    "madde": "MADDE",
    "gerekce": "GEREKCE",
    "cbk": "CBK",
    "bkk": "BKK",
    "yonetmelikler": "YONETMELIKLER",
    "teblig": "TEBLIG",
    "icgenelge": "ICGENELGE",
    "genelyazilar": "GENELYAZILAR",
    "ozelge": "OZELGE",
    "sirkuler": "SIRKULER",
}

KANUN_TURLERI = {
    "tumu": 99,
    "temel": 1,
    "yururlukteki_diger": 2,
    "degisiklik_yapan": 3,
    "mulga": 4,
}

_URL_RE = re.compile(r"/mevzuat/kanun/(\d+)(?:/([a-z]+)/(\d+))?")


class GibHatasi(ToolError):
    """Mesajı araç sonucu olarak modele gösterilen hata."""


def tur_dogrula(tur: str) -> str:
    t = tur.strip().lower()
    if t not in TURLER:
        raise GibHatasi(f"Bilinmeyen tür: {tur!r}. Geçerli türler: {', '.join(TURLER)}")
    return t


def url_coz(url: str) -> tuple[int, str | None, int | None]:
    """https://gib.gov.tr/mevzuat/kanun/436/ozelge/38597 -> (436, 'ozelge', 38597)"""
    m = _URL_RE.search(url)
    if not m:
        raise GibHatasi(f"Tanınmayan GİB mevzuat bağlantısı: {url}")
    kanun_id, tur, belge_id = m.groups()
    return int(kanun_id), tur, int(belge_id) if belge_id else None


def html_to_text(html: str | None) -> str:
    """Mevzuat HTML'ini okunur düz metne çevirir; tabloları satır satır korur."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tr in soup.find_all("tr"):
        cells: list[str] = []
        for c in tr.find_all(["td", "th"]):
            metin = c.get_text(" ", strip=True)
            # Birleştirilmiş hücreler bazen aynı metni tekrar ediyor
            if metin and (not cells or cells[-1] != metin):
                cells.append(metin)
        tr.replace_with(" | ".join(cells) + "\n")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(["p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6"]):
        block.insert_after("\n")
    text = soup.get_text()
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class GibClient:
    def __init__(self, timeout: float = 30.0) -> None:
        self._http = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=timeout,
            headers={
                "User-Agent": "Mozilla/5.0 (gib-mcp)",
                "Accept": "application/json",
            },
        )

        self._kilit = asyncio.Lock()
        self._son_istek = 0.0

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _gonder(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        # İstekleri sıraya sokup aralarına boşluk koyarak hız sınırına takılmayı azaltır.
        async with self._kilit:
            for bekleme in (*BEKLEMELER_429, None):
                gecen = time.monotonic() - self._son_istek
                if gecen < ISTEK_ARALIGI:
                    await asyncio.sleep(ISTEK_ARALIGI - gecen)
                try:
                    r = await self._http.request(method, path, **kwargs)
                except httpx.HTTPError as e:
                    raise GibHatasi(f"GİB sunucusuna ulaşılamadı: {e}") from e
                finally:
                    self._son_istek = time.monotonic()
                if r.status_code != 429 or bekleme is None:
                    break
                await asyncio.sleep(bekleme)
        if r.status_code == 429:
            raise GibHatasi("GİB hız sınırına takıldı (HTTP 429). Biraz bekleyip tekrar deneyin.")
        return r

    async def _istek(self, method: str, path: str, **kwargs: Any) -> Any:
        r = await self._gonder(method, path, **kwargs)
        try:
            data = r.json()
        except ValueError as e:
            raise GibHatasi(f"GİB beklenmeyen yanıt döndü (HTTP {r.status_code})") from e
        if r.status_code == 400:
            # GİB olmayan id'ler için de gövdesinde sadece "Hata!" olan 400 döndürüyor
            raise GibHatasi("GİB kaydı bulamadı ya da isteği geçersiz saydı (HTTP 400). Tür ve id'yi kontrol edin.")
        if r.status_code != 200 or data.get("status") != 200:
            mesaj = data.get("errorMessage") or data.get("statusMessage") or "bilinmeyen hata"
            raise GibHatasi(f"GİB hata döndü (HTTP {r.status_code}): {mesaj}")
        return data["resultContainer"]

    async def ara(self, sorgu: str, tur: str | None, sayfa: int, boyut: int) -> dict[str, Any]:
        if tur:
            t = tur_dogrula(tur)
            return await self._istek(
                "GET",
                "search/mevzuatWithEntityType",
                params={"term": sorgu, "entityType": ARAMA_TURLERI[t], "page": sayfa, "size": boyut},
            )
        gruplar = await self._istek("GET", "search/mevzuat", params={"term": sorgu})
        return {"gruplar": gruplar}

    async def son_eklenenler(self) -> list[dict[str, Any]]:
        sonuc = await self._istek("GET", "search/mevzuatLastAdded")
        return sonuc.get("results", [])

    async def kanunlar(self, kanun_turu: int) -> list[dict[str, Any]]:
        sonuc = await self._istek(
            "POST",
            "mevzuat/kanun/filterList",
            json={"kanunType": kanun_turu},
            params={"page": 0, "size": 1000, "sortFieldName": "priority", "sortType": "ASC"},
        )
        return sonuc.get("content", [])

    async def listele(
        self, tur: str, kanun_id: int | None, sayfa: int, boyut: int
    ) -> dict[str, Any]:
        t = tur_dogrula(tur)
        yol, siralama, yon, _ = TURLER[t]
        params = {"page": sayfa, "size": boyut, "sortFieldName": siralama, "sortType": yon}
        if t == "madde":
            if kanun_id is None:
                raise GibHatasi("Madde listesi için kanun_id gerekli.")
            params["kanunId"] = kanun_id
            params["status"] = 2
            return await self._istek("GET", "mevzuat/madde/findByKanunId", params=params)
        body: dict[str, Any] = {"status": 2, "deleted": False}
        if kanun_id is not None:
            body["kanunId"] = kanun_id
        return await self._istek("POST", f"mevzuat/{yol}/list", json=body, params=params)

    async def getir(self, tur: str, belge_id: int) -> dict[str, Any]:
        t = tur_dogrula(tur)
        yol = TURLER[t][0]
        params = {"maddeId": belge_id, "status": 2} if t == "madde" else {"id": belge_id}
        return await self._istek("GET", f"mevzuat/{yol}/findById", params=params)
