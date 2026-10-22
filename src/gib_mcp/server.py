"""GİB mevzuatı (gib.gov.tr/mevzuat) için MCP sunucusu."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from .client import DOSYA_URL, KANUN_TURLERI, TURLER, GibClient, html_to_text, url_coz

Tur = Literal[
    "madde", "gerekce", "cbk", "bkk", "yonetmelikler", "teblig",
    "icgenelge", "genelyazilar", "ozelge", "sirkuler",
]

TUR_ACIKLAMASI = "; ".join(f"{k} = {v[3]}" for k, v in TURLER.items())

# Kayıtlarda göze çarpan künye alanları, gösterim adlarıyla
KUNYE_ALANLARI = {
    "kanunTitle": "Kanun",
    "kanunNoTitle": "Kanun",
    "kanunNo": "Kanun no",
    "no": "Karar no",
    "ozelgeNo": "Özelge sayısı",
    "ozelgeTarih": "Özelge tarihi",
    "sirkulerNo": "Sirküler no",
    "sirkulerTarih": "Sirküler tarihi",
    "sayi": "Sayı",
    "tarih": "Tarih",
    "resmiGazeteTarih": "Resmî Gazete tarihi",
    "resmiGazeteSayi": "Resmî Gazete sayısı",
    "kanunKabulTarihi": "Kabul tarihi",
}

# Detay kayıtlarındaki ilişkili mevzuat listeleri
ILISKI_LISTELERI = {
    "maddeList": "madde",
    "ilgiliMaddeList": "madde",
    "tebliglerList": "teblig",
    "ozelgeList": "ozelge",
    "sirkulerList": "sirkuler",
    "yonetmeliklerList": "yonetmelikler",
    "genelYazilarList": "genelyazilar",
    "icGenelgelerList": "icgenelge",
    "cbkList": "cbk",
    "bkkList": "bkk",
    "gerekceList": "gerekce",
}


@asynccontextmanager
async def lifespan(_server: MCPServer) -> AsyncIterator[GibClient]:
    client = GibClient()
    try:
        yield client
    finally:
        await client.aclose()


mcp = MCPServer(
    "gib-mevzuat",
    instructions=(
        "Gelir İdaresi Başkanlığı (gib.gov.tr) vergi mevzuatına erişim: kanunlar, maddeler, "
        "tebliğler, özelgeler, sirkülerler, yönetmelikler, CBK/BKK kararları, iç genelgeler, "
        "genel yazılar ve gerekçeler. Tipik akış: mevzuat_ara ile bul, mevzuat_getir ile tam "
        "metni oku. Cevaplarda kaynağın gib.gov.tr bağlantısını belirt."
    ),
    lifespan=lifespan,
)

SALT_OKUNUR = ToolAnnotations(readOnlyHint=True, openWorldHint=True)


def _client(ctx: Context) -> GibClient:
    return ctx.request_context.lifespan_context


def _tarih(v: Any) -> Any:
    return v[:10] if isinstance(v, str) and len(v) >= 10 and v[4] == "-" else v


def _kunye(kayit: dict[str, Any]) -> list[str]:
    satirlar, gorulen = [], set()
    for alan, ad in KUNYE_ALANLARI.items():
        deger = kayit.get(alan)
        if deger in (None, "") or ad in gorulen:
            continue
        gorulen.add(ad)
        satirlar.append(f"{ad}: {_tarih(deger)}")
    return satirlar


def _site_link(kayit: dict[str, Any], tur: str, kanun_id: int | None = None) -> str | None:
    if kayit.get("siteLink"):
        return kayit["siteLink"]
    if kanun_id is not None:
        return f"https://gib.gov.tr/mevzuat/kanun/{kanun_id}/{tur}/{kayit['id']}"
    return None


def _ozet_satiri(kayit: dict[str, Any], tur: str, kanun_id: int | None = None) -> str:
    parcalar = [f"[{tur} #{kayit['id']}] {(kayit.get('title') or '').strip()}"]
    kunye = [s for s in _kunye(kayit) if not s.startswith("Kanun")]
    if kunye:
        parcalar.append("  " + " · ".join(kunye))
    link = _site_link(kayit, tur, kanun_id)
    if link:
        parcalar.append(f"  {link}")
    return "\n".join(parcalar)


def _sayfa_bilgisi(sonuc: dict[str, Any], sayfa: int) -> str:
    toplam = sonuc.get("totalElements")
    sayfa_sayisi = sonuc.get("totalPages")
    if toplam is None:
        return ""
    return f"Toplam {toplam} kayıt, sayfa {sayfa + 1}/{max(sayfa_sayisi or 1, 1)}"


@mcp.tool(annotations=SALT_OKUNUR)
async def mevzuat_ara(
    sorgu: Annotated[str, Field(description="Aranacak kelime veya ifade, ör. 'kira geliri stopaj'")],
    ctx: Context,
    tur: Annotated[
        Tur | None,
        Field(description=f"Sadece bu türde ara. Boş bırakılırsa tüm türlerde arar. {TUR_ACIKLAMASI}"),
    ] = None,
    sayfa: Annotated[int, Field(ge=0, description="Sayfa numarası (0'dan başlar), sadece tur verilince")] = 0,
    boyut: Annotated[int, Field(ge=1, le=50, description="Sayfa başına sonuç, sadece tur verilince")] = 10,
) -> str:
    """GİB mevzuatında tam metin arama yapar. Sonuçlardaki tür ve id ile mevzuat_getir çağrılabilir."""
    sonuc = await _client(ctx).ara(sorgu, tur, sayfa, boyut)
    satirlar: list[str] = []
    if tur:
        satirlar.append(_sayfa_bilgisi(sonuc, sayfa))
        ogeler = [(tur, o) for o in sonuc.get("content", [])]
    else:
        ogeler = [
            (grup["entityName"].lower(), o)
            for grup in sonuc["gruplar"]
            for o in grup.get("values", [])
        ]
    for tur_adi, o in ogeler:
        satirlar.append(_arama_satiri(tur_adi, o))
    if not ogeler:
        return f"'{sorgu}' için sonuç bulunamadı."
    return "\n\n".join(s for s in satirlar if s)


def _arama_satiri(tur_adi: str, o: dict[str, Any]) -> str:
    url = o.get("url") or ""
    belge = ""
    try:
        _, url_turu, belge_id = url_coz(url)
        if url_turu and belge_id:
            belge = f"[{url_turu} #{belge_id}] "
    except Exception:
        belge = f"[{tur_adi}] "
    satir = f"{belge}{(o.get('text') or '').strip()}"
    ek = []
    if o.get("kanunTitle"):
        ek.append(f"{o['kanunTitle']} ({o.get('kanunNo')})")
    if o.get("resmiGazeteTarih"):
        ek.append(f"RG: {_tarih(o['resmiGazeteTarih'])}")
    if ek:
        satir += "\n  " + " · ".join(ek)
    if url:
        satir += f"\n  {url}"
    return satir


@mcp.tool(annotations=SALT_OKUNUR)
async def son_eklenen_mevzuat(ctx: Context) -> str:
    """gib.gov.tr'ye en son eklenen mevzuatı (özelge, tebliğ, CBK vb.) listeler."""
    sonuclar = await _client(ctx).son_eklenenler()
    if not sonuclar:
        return "Son eklenen mevzuat bulunamadı."
    return "\n\n".join(_arama_satiri(o.get("entityType", "").lower(), o) for o in sonuclar)


@mcp.tool(annotations=SALT_OKUNUR)
async def kanunlari_listele(
    ctx: Context,
    kanun_turu: Annotated[
        Literal["tumu", "temel", "yururlukteki_diger", "degisiklik_yapan", "mulga"],
        Field(description="Kanun grubu"),
    ] = "temel",
) -> str:
    """GİB'in yayımladığı vergi kanunlarını kanun_id'leriyle listeler.
    kanun_id, mevzuat_listele ile bir kanuna bağlı madde, tebliğ, özelge vb. listelemek için kullanılır."""
    kanunlar = await _client(ctx).kanunlar(KANUN_TURLERI[kanun_turu])
    satirlar = [f"{len(kanunlar)} kanun:"]
    for k in kanunlar:
        bilgi = [f"No {k['kanunNo']}"] if k.get("kanunNo") else []
        if k.get("kanunKabulTarihi"):
            bilgi.append(f"kabul {_tarih(k['kanunKabulTarihi'])}")
        satirlar.append(f"kanun_id={k['id']}: {k['title'].strip()} ({', '.join(bilgi)})")
    return "\n".join(satirlar)


@mcp.tool(annotations=SALT_OKUNUR)
async def mevzuat_listele(
    tur: Annotated[Tur, Field(description=f"Listelenecek mevzuat türü. {TUR_ACIKLAMASI}")],
    ctx: Context,
    kanun_id: Annotated[
        int | None,
        Field(description="Sadece bu kanuna bağlı kayıtlar (kanunlari_listele'den). 'madde' için zorunlu."),
    ] = None,
    sayfa: Annotated[int, Field(ge=0, description="Sayfa numarası (0'dan başlar)")] = 0,
    boyut: Annotated[int, Field(ge=1, le=100, description="Sayfa başına kayıt")] = 20,
) -> str:
    """Bir mevzuat türündeki kayıtları en yeniden eskiye (maddelerde sırasıyla) listeler.
    Tam metin için mevzuat_getir kullanın."""
    sonuc = await _client(ctx).listele(tur, kanun_id, sayfa, boyut)
    kayitlar = sonuc.get("content", [])
    if not kayitlar:
        return "Kayıt bulunamadı."
    satirlar = [_sayfa_bilgisi(sonuc, sayfa)]
    satirlar += [_ozet_satiri(k, tur, kanun_id) for k in kayitlar]
    return "\n\n".join(s for s in satirlar if s)


@mcp.tool(annotations=SALT_OKUNUR)
async def mevzuat_getir(
    ctx: Context,
    tur: Annotated[Tur | None, Field(description=f"Belge türü. {TUR_ACIKLAMASI}")] = None,
    id: Annotated[int | None, Field(description="Belge id'si (arama/liste sonuçlarındaki #numara)")] = None,
    url: Annotated[
        str | None,
        Field(description="tur/id yerine gib.gov.tr bağlantısı, ör. https://gib.gov.tr/mevzuat/kanun/436/ozelge/38597"),
    ] = None,
    baslangic: Annotated[
        int, Field(ge=0, description="Uzun metinlerde okumaya başlanacak karakter konumu")
    ] = 0,
    uzunluk: Annotated[int, Field(ge=1000, le=200_000, description="Döndürülecek en fazla karakter")] = 40_000,
) -> str:
    """Bir mevzuat belgesinin (özelge, tebliğ, madde vb.) tam metnini, künyesini, eklerini ve
    ilişkili mevzuatı getirir. Metin uzunsa 'baslangic' ile devamı okunabilir."""
    if url:
        _, url_turu, url_id = url_coz(url)
        if not url_turu or url_id is None:
            return "Bu bağlantı bir kanun sayfası; maddeler için mevzuat_listele(tur='madde', kanun_id=...) kullanın."
        tur, id = url_turu, url_id  # type: ignore[assignment]
    if not tur or id is None:
        return "tur ve id, ya da url verilmelidir."

    kayit = await _client(ctx).getir(tur, id)
    metin = html_to_text(kayit.get("description"))

    bolumler = [f"# {(kayit.get('title') or '').strip()}", f"Tür: {TURLER[tur][3]} (#{kayit['id']})"]
    bolumler += _kunye(kayit)
    if link := _site_link(kayit, tur):
        bolumler.append(f"Kaynak: {link}")
    if kayit.get("summary"):
        bolumler.append(f"\nÖzet: {html_to_text(kayit['summary'])}")

    dosyalar = [f for f in kayit.get("fileNames") or [] if f]
    if dosyalar:
        bolumler.append("\nEkler:\n" + "\n".join(f"- {DOSYA_URL}{f}" for f in dosyalar))

    toplam = len(metin)
    parca = metin[baslangic : baslangic + uzunluk]
    bolumler.append("\n## Metin\n" + (parca or "(Metin yok; içerik eklerde olabilir.)"))
    if baslangic + uzunluk < toplam:
        bolumler.append(
            f"\n[Metin kesildi: {baslangic}-{baslangic + len(parca)} / {toplam} karakter. "
            f"Devamı için baslangic={baslangic + len(parca)} ile tekrar çağırın.]"
        )

    iliskiler = []
    for alan, iliskili_tur in ILISKI_LISTELERI.items():
        for o in (kayit.get(alan) or [])[:15]:
            if isinstance(o, dict) and o.get("id"):
                ad = (o.get("title") or "").strip()
                iliskiler.append(f"- [{iliskili_tur} #{o['id']}] {ad}")
    if iliskiler:
        bolumler.append("\n## İlişkili mevzuat\n" + "\n".join(iliskiler))
    return "\n".join(bolumler)


def main() -> None:
    # stdio istemcisinin log panelini her HTTP isteğiyle doldurmasın
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # Railway gibi platformlar PORT verir: o zaman HTTP üzerinden /mcp adresinde yayın yap
    port = os.environ.get("PORT")
    if port:
        mcp.run(transport="streamable-http", host="0.0.0.0", port=int(port), stateless_http=True)
    else:
        mcp.run()
