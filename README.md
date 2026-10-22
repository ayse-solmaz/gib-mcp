# gib-mcp

[gib.gov.tr/mevzuat](https://gib.gov.tr/mevzuat) üzerindeki vergi mevzuatını okuyan bir MCP sunucusu. Claude gibi istemciler kanunları, maddeleri, tebliğleri, özelgeleri, sirkülerleri, yönetmelikleri, CBK/BKK kararlarını, iç genelgeleri, genel yazıları ve gerekçeleri arayıp tam metinlerini okuyabilir.

## Nasıl çalışıyor

gib.gov.tr bir Next.js uygulaması. Sayfa HTML'inde içerik yok; tarayıcı veriyi `https://gib.gov.tr/api/gibportal/...` altındaki JSON uç noktalarından çekiyor. Bu uç noktalar belgelenmemiş; sitenin JS paketlerinden çıkarıldı. Kimlik doğrulama gerekmiyor.

| Amaç | Uç nokta |
|---|---|
| Genel arama | `GET search/mevzuat?term=` |
| Türe göre arama (sayfalı) | `GET search/mevzuatWithEntityType?term=&entityType=&page=&size=` |
| Son eklenenler | `GET search/mevzuatLastAdded` |
| Kanun listesi | `POST mevzuat/kanun/filterList` `{"kanunType": 99}` |
| Kanun maddeleri | `GET mevzuat/madde/findByKanunId?kanunId=` |
| Tür listesi | `POST mevzuat/{tur}/list` `{"status":2,"deleted":false,"kanunId":…}` |
| Tek belge | `GET mevzuat/{tur}/findById?id=` (madde için `maddeId=`) |
| Ek dosyalar (PDF) | `https://cdn.gib.gov.tr/api/gibportal-file/file/getFile?objectKey=` |

Belge metinleri HTML olarak geliyor; sunucu bunları tablolar korunarak düz metne çeviriyor. Site kısa sürede çok istek gelince `429` döndürüyor. Bu yüzden istemci istekleri sıraya sokuyor ve 429 aldığında bekleyip tekrar deniyor.

## Araçlar

| Araç | Ne yapar |
|---|---|
| `mevzuat_ara` | Tam metin arama; isteğe bağlı olarak türe göre filtreler ve sayfalar |
| `son_eklenen_mevzuat` | Siteye en son eklenen mevzuat |
| `kanunlari_listele` | Kanunları `kanun_id`'leriyle listeler (temel, yürürlükteki diğer, değişiklik yapan, mülga, tümü) |
| `mevzuat_listele` | Bir türü (isteğe bağlı olarak tek bir kanuna göre) listeler |
| `mevzuat_getir` | Belgenin tam metni, künyesi, ekleri ve ilişkili mevzuatı; `tur`+`id` ya da gib.gov.tr bağlantısıyla çalışır |

Türler: `madde`, `gerekce`, `cbk`, `bkk`, `yonetmelikler`, `teblig`, `icgenelge`, `genelyazilar`, `ozelge`, `sirkuler`.

## Kurulum

Gereken tek şey [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

### Claude Code

```bash
claude mcp add gib-mevzuat -- uv run --directory C:/Users/aysnu/Projects/gib-mcp gib-mcp
```

### Claude Desktop

`%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "gib-mevzuat": {
      "command": "uv",
      "args": ["run", "--directory", "C:/Users/aysnu/Projects/gib-mcp", "gib-mcp"]
    }
  }
}
```

## Railway'e yükleme

`PORT` ortam değişkeni varsa sunucu stdio yerine HTTP'de (`/mcp`) açılır; Railway bu değişkeni kendisi veriyor. Başlatma komutu `railway.json` içinde tanımlı.

1. Repoyu GitHub'a yükle.
2. Railway'de **New Project → Deploy from GitHub repo** ile bu repoyu seç.
3. Servisin **Settings → Networking** bölümünden **Generate Domain** ile bir adres al.
4. Claude Code'a ekle:

```bash
claude mcp add --transport http gib-mevzuat https://<railway-adresin>/mcp
```

Sunucuda kimlik doğrulama yok: adresi bilen herkes kullanabilir. Veriler zaten herkese açık ve araçlar sadece okuma yapıyor; ama herkes aynı GİB hız sınırını paylaşır.

## Notlar

- API resmî değil; GİB sitesini değiştirirse uç noktalar bozulabilir.
- Bazı belgelerin (ör. CBK kararları) asıl metni HTML'de değil, ekteki PDF'te. `mevzuat_getir` PDF bağlantısını veriyor ama PDF'i okumuyor.
