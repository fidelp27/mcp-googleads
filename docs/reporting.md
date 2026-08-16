# Guía de reporting — UTMs y resolvedores

Cómo se capturan los datos de las campañas vía UTMs y cómo traducir los IDs
crudos (geo, ad group, anuncio) a nombres legibles para tus reportes.

---

## 1. Esquema de UTMs

El tracking se configura como **Final URL suffix a nivel campaña** (campo
`campaign.final_url_suffix`). Aplica automáticamente a todos los anuncios de la
campaña, es compatible con parallel tracking y no modifica las URLs finales.

Se setea con el tool `update_campaign`:

```json
update_campaign(customer_id, campaign_id, updates={"final_url_suffix": "<suffix>"})
```

Suffix aplicado a la campaña de Clariola:

```
utm_source=google&utm_medium=cpc&utm_campaign={campaignid}&utm_content={adgroupid}&utm_term={keyword}&utm_creative={creative}&utm_matchtype={matchtype}&utm_network={network}&utm_device={device}&utm_geo={loc_physical_ms}
```

### Qué captura cada parámetro

| Parámetro | ValueTrack | Dato | Legible directo |
|---|---|---|---|
| `utm_source` | `google` | fuente (fija) | ✅ |
| `utm_medium` | `cpc` | medio pago (fijo) | ✅ |
| `utm_campaign` | `{campaignid}` | ID de campaña | ⚠️ ID (ver §3) |
| `utm_content` | `{adgroupid}` | ID del ad group | ❌ resolver (§3) |
| `utm_term` | `{keyword}` | keyword que disparó el clic | ✅ |
| `utm_creative` | `{creative}` | ID del anuncio | ❌ resolver (§3) |
| `utm_matchtype` | `{matchtype}` | concordancia: `e`/`p`/`b` | ✅ |
| `utm_network` | `{network}` | red: `g` búsqueda, `s` partners, `d` display | ✅ |
| `utm_device` | `{device}` | dispositivo: `m`/`c`/`t` | ✅ |
| `utm_geo` | `{loc_physical_ms}` | ubicación física del usuario | ❌ resolver (§2) |

**Estándar GA4** (dimensiones nativas): `utm_source`, `utm_medium`,
`utm_campaign`, `utm_content`, `utm_term`. El resto son **custom** — para verlos
en GA4 hay que crear *dimensiones personalizadas* basadas en esos parámetros de
URL, o tomarlos en tu propia analítica/BI.

---

## 2. Resolver `utm_geo` → ubicación

`{loc_physical_ms}` es un ID numérico de ubicación (ciudad/región/país), no un
nombre. Son miles de valores posibles, así que se resuelven bajo demanda con:

```bash
# IDs sueltos
python scripts/resolve_geo.py 2032 1000073 20037

# Desde un export (columna utm_geo) -> CSV para el reporte
python scripts/resolve_geo.py --file reporte.csv --column utm_geo --out output/geo_mapping.csv
```

Devuelve: `id, name, canonical_name, country_code, target_type, status`.

> **Usá siempre `canonical_name`.** Hay muchos "Lima", "Santiago", "Córdoba" en
> distintos países. `name` solo es ambiguo; `canonical_name` desambigua
> (ej. `Lima,New York,United States` vs `Lima,Lima Region,Peru`).

`geo_target_constant` es global: se puede consultar desde cualquier cuenta
(incluido el MCC), por eso `resolve_geo.py` no necesita `--customer-id`.

---

## 3. Resolver `utm_content` / `utm_creative` → nombres

`utm_content={adgroupid}` y `utm_creative={creative}` (ID de anuncio) son
**específicos de la cuenta cliente**, no globales.

```bash
# IDs sueltos
python scripts/resolve_ads.py --customer-id 5461642152 \
    --adgroups 199327068917 --ads 821049505825

# Desde un export, resolviendo ambas columnas -> dos CSV
python scripts/resolve_ads.py --customer-id 5461642152 \
    --file reporte.csv --adgroup-column utm_content --ad-column utm_creative \
    --out-prefix output/clariola
```

Genera:
- `<prefix>_adgroups.csv`: `ad_group_id, ad_group_name, ad_group_status, campaign_id, campaign_name`
- `<prefix>_ads.csv`: `ad_id, ad_type, ad_status, ad_group_id, ad_group_name, campaign_name, first_headline`

> **`--customer-id` es la cuenta CLIENTE**, no el MCC. Los ad groups y anuncios
> viven en la cuenta cliente (ej. Clariola `5461642152`); el manager
> (`7384467839`) no tiene ads y devolvería `NOT_FOUND`.

`utm_campaign={campaignid}` también resuelve con este script (aparece como
`campaign_id`/`campaign_name` en la salida de ad groups).

---

## 4. Flujo end-to-end

1. **Exportá** de GA4 / Google Ads con las columnas UTM (`utm_geo`,
   `utm_content`, `utm_creative`, etc.) junto a las métricas.
2. **Resolvé** los IDs a nombres:
   ```bash
   python scripts/resolve_geo.py --file reporte.csv --column utm_geo --out output/geo_mapping.csv
   python scripts/resolve_ads.py --customer-id 5461642152 --file reporte.csv \
       --adgroup-column utm_content --ad-column utm_creative --out-prefix output/clariola
   ```
3. **Uní** (`JOIN`) por ID en tu BI/planilla:
   - `utm_geo` ↔ `geo_mapping.id`
   - `utm_content` ↔ `clariola_adgroups.ad_group_id`
   - `utm_creative` ↔ `clariola_ads.ad_id`
4. Reporte final con ubicación, ad group y anuncio legibles junto a las métricas.

---

## 5. Requisitos y notas

- Los scripts usan las mismas credenciales `.env` que el servidor MCP y respetan
  la whitelist `ALLOWED_CUSTOMER_IDS`.
- **Auto-tagging (`gclid`)**: si está activo (recomendado), GA importa además
  CPC real, posición, etc. Convive sin problemas con estas UTMs.
- Para que `utm_campaign` muestre el **nombre** en vez del ID se pueden usar
  parámetros personalizados, pero el ID es más robusto (no se rompe al renombrar).
- Los IDs se cachean como datos siempre frescos: no hay tabla estática que
  mantener, se resuelve contra la API cuando lo necesitás.
