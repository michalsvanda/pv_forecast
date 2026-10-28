"""
pv_forecast.py
==============
Hourly photovoltaic power prediction for today and tomorrow.

Dependencies (install once):
    pip install pvlib requests pandas

APIs used (both free, no key required):
    - Open-Meteo  https://open-meteo.com  — solar irradiance & weather forecast
    - pvlib       https://pvlib-python.readthedocs.io — PV physics

Public interface
----------------
    fetch_openmeteo_data(latitude, longitude, altitude_m)
        → DataFrame with GHI, DNI, DHI, temperature and sun-position columns,
          indexed by UTC timestamp.  Independent of any panel orientation.

    compute_pv_power(weather_df, peak_power_wp, tilt_deg,
                     azimuth_from_north_deg, ...)
        → DataFrame with columns [time, power_w] only.
"""

import requests
import pandas as pd
import pvlib
from pvlib import location, irradiance, atmosphere


# ---------------------------------------------------------------------------
# 1.  Data-fetching function  (location only, panel-agnostic)
# ---------------------------------------------------------------------------

def fetch_openmeteo_data(
    latitude: float,
    longitude: float,
    altitude_m: float,
) -> pd.DataFrame:
    """
    Fetch hourly solar irradiance, air temperature, and sun-position data
    for today and tomorrow from the Open-Meteo free forecast API.

    The returned DataFrame contains everything needed to compute POA
    irradiance for any panel orientation — no panel parameters are required
    here, so the same fetch result can be reused for multiple panels at the
    same site without a second network call.

    Parameters
    ----------
    latitude   : Geographic latitude  in decimal degrees (+N / -S).
    longitude  : Geographic longitude in decimal degrees (+E / -W).
    altitude_m : Altitude above sea level in metres.

    Returns
    -------
    pd.DataFrame indexed by UTC-aware datetime (hourly) with columns:
        ghi            — Global Horizontal Irradiance     (W/m²)
        dni            — Direct Normal Irradiance          (W/m²)
        dhi            — Diffuse Horizontal Irradiance     (W/m²)
        temperature_2m — Air temperature at 2 m           (°C)
        solar_zenith   — Apparent solar zenith angle       (°)
        solar_azimuth  — Solar azimuth, N=0 clockwise      (°)
        airmass        — Relative airmass                  (-)
        dni_extra      — Extraterrestrial DNI              (W/m²)
    """

    # --- Open-Meteo request ------------------------------------------------
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": [
            "shortwave_radiation",       # GHI  (W/m²)
            "direct_normal_irradiance",  # DNI  (W/m²)
            "diffuse_radiation",         # DHI  (W/m²)
            "temperature_2m",            # °C
        ],
        "forecast_days": 3,
        "timezone": "UTC",
    }

    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    hourly = resp.json()["hourly"]

    df = pd.DataFrame({
        "ghi":            hourly["shortwave_radiation"],
        "dni":            hourly["direct_normal_irradiance"],
        "dhi":            hourly["diffuse_radiation"],
        "temperature_2m": hourly["temperature_2m"],
    }, index=pd.to_datetime(hourly["time"], utc=True))
    df.index.name = "time"

    # --- Sun position (pvlib) ----------------------------------------------
    site = location.Location(
        latitude=latitude,
        longitude=longitude,
        altitude=altitude_m,
        tz="UTC",
    )
    solar_pos = site.get_solarposition(df.index)

    df["solar_zenith"]  = solar_pos["apparent_zenith"]
    df["solar_azimuth"] = solar_pos["azimuth"]   # pvlib: N=0, clockwise

    # --- Auxiliary quantities used by transposition models -----------------
    df["airmass"]   = atmosphere.get_relative_airmass(df["solar_zenith"])
    df["dni_extra"] = pvlib.irradiance.get_extra_radiation(df.index)

    return df


# ---------------------------------------------------------------------------
# 2.  Power-computation function  (panel-specific, uses fetched DataFrame)
# ---------------------------------------------------------------------------

def compute_pv_power(
    weather_df: pd.DataFrame,
    peak_power_wp: float,
    tilt_deg: float,
    azimuth_from_north_deg: float,
    performance_ratio: float = 0.80,
    temp_coefficient: float = -0.004,
    t_ref: float = 25.0,
    noct: float = 48.0,
) -> pd.DataFrame:
    """
    Compute hourly AC power output for a single PV panel.

    Parameters
    ----------
    weather_df              : DataFrame returned by fetch_openmeteo_data().
    peak_power_wp           : Panel peak power in Watt-peak (Wp).
    tilt_deg                : Panel tilt from horizontal plane (°).
                              0 = flat, 90 = vertical.
    azimuth_from_north_deg  : Panel azimuth measured clockwise from North (°).
                              0/360 = N, 90 = E, 180 = S, 270 = W.
    performance_ratio       : System losses — inverter, wiring, soiling, ...
                              (default 0.80 = 80 %).
    temp_coefficient        : Power temperature coefficient per °C
                              (default -0.004, i.e. -0.4 %/°C, typical silicon).
    t_ref                   : STC reference cell temperature (default 25 °C).
    noct                    : Nominal Operating Cell Temperature (default 48 °C).

    Returns
    -------
    pd.DataFrame with columns:
        time     — UTC-aware datetime (hourly)
        power_w  — Estimated AC power output (W), floored at 0
    """

    df = weather_df.copy()

    # pvlib >= 0.9 uses the compass convention (N=0, clockwise), same as our
    # input, so no conversion is needed.
    surface_azimuth = azimuth_from_north_deg % 360

    # --- Plane-of-Array irradiance (Perez transposition model) -------------
    poa = irradiance.get_total_irradiance(
        surface_tilt=tilt_deg,
        surface_azimuth=surface_azimuth,
        solar_zenith=df["solar_zenith"],
        solar_azimuth=df["solar_azimuth"],
        dni=df["dni"],
        ghi=df["ghi"],
        dhi=df["dhi"],
        dni_extra=df["dni_extra"],
        airmass=df["airmass"],
        model="perez",
    )
    poa_total = poa["poa_global"].clip(lower=0)

    # --- Cell temperature (NOCT model) -------------------------------------
    # T_cell = T_air + (POA / 800) * (NOCT - 20)
    t_cell = df["temperature_2m"] + (poa_total / 800.0) * (noct - 20.0)

    # --- Temperature correction factor -------------------------------------
    temp_factor = 1.0 + temp_coefficient * (t_cell - t_ref)

    # --- AC power output ---------------------------------------------------
    power_w = (
        (poa_total / 1000.0)
        * peak_power_wp
        * performance_ratio
        * temp_factor
    ).clip(lower=0).round(1)

    return pd.DataFrame({
        "time":    df.index,
        "power_w": power_w.values,
    })


# ---------------------------------------------------------------------------
# Pretty-print helper
# ---------------------------------------------------------------------------

def print_forecast(df: pd.DataFrame, local_tz: str = "UTC") -> None:
    """Print a compact daily summary of the power forecast."""
    df2 = df.copy()
    df2["time_local"] = df2["time"].dt.tz_convert(local_tz)
    df2["date"] = df2["time_local"].dt.date

    for day, group in df2.groupby("date"):
        print(f"\n{'='*40}")
        print(f"  {day}  ({local_tz})")
        print(f"{'='*40}")
        print(f"  {'Hour':>5}  {'Power (W)':>10}")
        print(f"  {'-'*18}")
        for _, row in group.iterrows():
            print(f"  {row['time_local'].strftime('%H:%M'):>5}  {row['power_w']:>10.1f}")
        print(f"\n  Daily yield : {group['power_w'].sum() / 1000:.3f} kWh")
        print(f"  Peak power  : {group['power_w'].max():.0f} W")


# ---------------------------------------------------------------------------
# Example — edit values and run:  python pv_forecast.py
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    # Step 1 — fetch once for the site (panel-agnostic)
    weather = fetch_openmeteo_data(
        latitude=50.0,    # get coordinates and elevation for your location
        longitude=15.0,
        altitude_m=200,
    )
    # two strings, each having 10 panels
    string1_panels=10
    string2_panels=10
    # Step 2 — compute power for a specific panel
    # for panels in the first string
    string1 = compute_pv_power(weather_df=weather,peak_power_wp=450,tilt_deg=35,azimuth_from_north_deg=182)
    # second string has two parts with a different tilt; compute for each first
    string2a = compute_pv_power(weather_df=weather,peak_power_wp=535,tilt_deg=35,azimuth_from_north_deg=182)
    string2b = compute_pv_power(weather_df=weather,peak_power_wp=535,tilt_deg=15,azimuth_from_north_deg=182)
    # then merge
    merged_string2 = pd.merge(string2a, string2b, on="time", suffixes=("_1", "_2"))
    # the panels are in the series, hence the PV output is given by the weakest one
    string2 = pd.DataFrame({"time": merged_string2["time"],"power_w": merged_string2[["power_w_1", "power_w_2"]].min(axis=1)})
    
    # helper dataframe, both string joined together
    junk=pd.merge(string1, string2, on="time", suffixes=("_1", "_2"))
    
    # computing the total power of the string combinations
    total=pd.DataFrame({"time": junk["time"],"power_w": junk["power_w_1"]*string1_panels+junk["power_w_2"]*string2_panels})
    total["date"] = total["time"].dt.date
    total.groupby('date')['power_w'].sum().reset_index()
    
    #string1["date"] = total["time"].dt.date
    #string1["power_w"]=10*string1["power_w"]
    #string1.groupby('date')['power_w'].sum().reset_index()
    #import code
    #code.interact(local=locals())
    
    

    # print
    print_forecast(total, local_tz="Europe/Prague")

    # The weather fetch can be reused for additional panels at the same site
    # without any extra network calls, for example:
    # forecast_east = compute_pv_power(weather, 300, tilt_deg=30, azimuth_from_north_deg=90)

    # Save to CSV if needed:
    # forecast.to_csv("pv_forecast.csv", index=False)
