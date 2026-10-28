# pv_forecast

Hodinová předpověď výkonu fotovoltaické elektrárny (FVE) na dnešek a zítřek.

Skript stáhne předpověď slunečního záření a teploty z bezplatné služby
[Open-Meteo](https://open-meteo.com/) a pomocí knihovny
[pvlib](https://pvlib-python.readthedocs.io/) z ní spočítá očekávaný výkon
panelů s danou orientací a sklonem. Nevyžaduje žádný API klíč.

## Jak to funguje

Výpočet je rozdělen do dvou kroků, aby bylo možné jedno stažení dat použít
pro libovolný počet různě orientovaných panelů na stejném místě.

### 1. Data o počasí – `fetch_openmeteo_data()`

Funkce pro zadanou zeměpisnou polohu a nadmořskou výšku stáhne z Open-Meteo
hodinovou předpověď na 3 dny (v UTC) a doplní ji o polohu Slunce:

| Sloupec          | Význam                                         | Zdroj       |
|------------------|------------------------------------------------|-------------|
| `ghi`            | globální horizontální ozáření (W/m²)           | Open-Meteo  |
| `dni`            | přímé normálové ozáření (W/m²)                 | Open-Meteo  |
| `dhi`            | difúzní horizontální ozáření (W/m²)            | Open-Meteo  |
| `temperature_2m` | teplota vzduchu ve 2 m (°C)                    | Open-Meteo  |
| `solar_zenith`   | zdánlivý zenitový úhel Slunce (°)              | pvlib       |
| `solar_azimuth`  | azimut Slunce, sever = 0°, po směru hodin (°)  | pvlib       |
| `airmass`        | relativní vzduchová hmota (–)                  | pvlib       |
| `dni_extra`      | mimoatmosférické přímé ozáření (W/m²)          | pvlib       |

Výsledek nezávisí na parametrech panelů.

### 2. Výkon panelu – `compute_pv_power()`

Pro jeden panel se zadaným špičkovým výkonem, sklonem a azimutem se postupně
počítá:

1. **Ozáření v rovině panelu (POA)** – transpozice GHI/DNI/DHI do roviny
   panelu Perezovým modelem (`pvlib.irradiance.get_total_irradiance`,
   `model="perez"`). Zahrnuje přímou, difúzní i od země odraženou složku
   (výchozí albedo pvlib 0,25).
2. **Teplota článku** – zjednodušený model NOCT:

   T_článek = T_vzduch + (POA / 800) · (NOCT − 20)

3. **Teplotní korekce výkonu** – lineární:

   k_T = 1 + γ · (T_článek − 25 °C), typicky γ = −0,4 %/°C

4. **Výstupní výkon**:

   P = (POA / 1000) · P_peak · PR · k_T

   kde PR (performance ratio, výchozí 0,80) souhrnně zahrnuje ztráty na
   střídači, kabeláži, znečištění apod. Výsledek je oříznut na nezáporné
   hodnoty.

Funkce vrací tabulku se sloupci `time` (UTC) a `power_w` (W) pro jeden panel.

### Sériové zapojení stringů

Ukázka v bloku `if __name__ == "__main__":` modeluje dva stringy po
10 panelech:

- **string 1** – panely 450 Wp, sklon 35°, azimut 182°,
- **string 2** – panely 535 Wp, část se sklonem 35° a část se sklonem 15°,
  azimut 182°.

Protože jsou panely ve stringu zapojeny sériově, proud celého stringu je
omezen nejslabším panelem. Pro string 2 se proto v každé hodině bere menší
z výkonů obou orientací a násobí se počtem panelů. Celkový výkon je součet
obou stringů. Jde o zjednodušení – neuvažuje vliv bypass diod ani chování
MPPT sledovače střídače.

### Výstup

Funkce `print_forecast()` vypíše hodinový výkon po dnech v místním čase
a pro každý den denní výrobu (součet hodinových průměrů, kWh) a špičkový
výkon.

## Instalace

```bash
git clone https://github.com/michalsvanda/pv_forecast.git
cd pv_forecast
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Skript potřebuje přístup k internetu (API Open-Meteo).

## Použití

### Spuštění ukázky

V bloku `if __name__ == "__main__":` upravte souřadnice, nadmořskou výšku
a parametry svých panelů a spusťte:

```bash
python pv_forecast.py
```

Výstup (zkráceno, hodnoty jsou ilustrační):

```
========================================
  2026-10-28  (Europe/Prague)
========================================
   Hour   Power (W)
  ------------------
  ...
  12:00      6240.5
  13:00      6012.3
  ...

  Daily yield : 31.512 kWh
  Peak power  : 6241 W
```

### Použití jako modul

```python
from pv_forecast import fetch_openmeteo_data, compute_pv_power, print_forecast

# jedno stažení dat pro dané místo
weather = fetch_openmeteo_data(latitude=50.08, longitude=14.42, altitude_m=250)

# výkon jednoho panelu 450 Wp, sklon 35°, orientace na jih
panel = compute_pv_power(weather, peak_power_wp=450,
                         tilt_deg=35, azimuth_from_north_deg=180)

# 12 panelů → výkon celé řady
array = panel.assign(power_w=panel["power_w"] * 12)
print_forecast(array, local_tz="Europe/Prague")

# stejná data lze bez dalšího síťového dotazu použít i pro jinou orientaci
east = compute_pv_power(weather, 300, tilt_deg=30, azimuth_from_north_deg=90)

# uložení do CSV
array.to_csv("pv_forecast.csv", index=False)
```

### Parametry `compute_pv_power()`

| Parametr                 | Výchozí  | Význam                                            |
|--------------------------|----------|---------------------------------------------------|
| `peak_power_wp`          | –        | špičkový výkon panelu (Wp)                        |
| `tilt_deg`               | –        | sklon od vodorovné roviny (0 = vodorovně, 90 = svisle) |
| `azimuth_from_north_deg` | –        | azimut od severu po směru hodin (90 = V, 180 = J, 270 = Z) |
| `performance_ratio`      | 0,80     | souhrnné systémové ztráty                         |
| `temp_coefficient`       | −0,004   | teplotní koeficient výkonu (1/°C)                 |
| `t_ref`                  | 25,0     | referenční teplota článku STC (°C)                |
| `noct`                   | 48,0     | jmenovitá provozní teplota článku (°C)            |

Hodnoty teplotního koeficientu a NOCT najdete v katalogovém listu panelu.

## Omezení modelu

- Přesnost je dána především přesností předpovědi počasí (oblačnost).
- Hodinové hodnoty záření z Open-Meteo jsou průměrem za předchozí hodinu,
  kdežto poloha Slunce se počítá v okamžiku časové značky. Kolem východu
  a západu Slunce to může způsobit znatelnou odchylku.
- Neuvažuje se stínění, sníh, úhlová odrazivost krycího skla (IAM),
  spektrální vlivy, degradace panelů ani výkonové omezení střídače.
- Teplotní model NOCT je zjednodušený a nezohledňuje rychlost větru.

## Licence

Projekt je šířen pod licencí MIT – viz soubor [LICENSE](LICENSE).
