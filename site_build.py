#!/usr/bin/env python3
"""
site_build.py - rakentaa selattavan hintasivun (staattinen HTML + JSON) kannasta.

  python site_build.py --out _site            # Turso, jos TURSO_*-muuttujat on asetettu
  python site_build.py --db flights.db --out _site
  python -m http.server -d _site 8000          # katselu paikallisesti: http://localhost:8000

Rakennus tiivistää jokaisen keruukerran halvimmat hinnat tauluihin fare_min ja dest_min,
joten joka yö luetaan kannasta vain uusin keruu ja tiivistetty historia, ei koko fares-taulua.
Sivu itse on web/index.html; data kirjoitetaan kansioon <out>/data/.
"""
import argparse
import datetime as dt
import json
import os
import shutil
import sys

from flightwatch import init_db, load_config

HERE = os.path.dirname(os.path.abspath(__file__))
HISTORY_RUNS = 60  # montako viimeisintä keruuta näytetään lähtöpäiväkohtaisessa historiassa


def trip_key(n):
    return "ow" if n is None else str(int(n))


def ret_date(out, n):
    return None if n is None else (dt.date.fromisoformat(out) + dt.timedelta(days=int(n))).isoformat()


def gf_tfs(o, d, out, ret):
    """Google Flightsin täsmähaun tfs-parametri (sama haku kuin keruussa). Sivu tekee siitä linkin
    https://www.google.com/travel/flights?tfs=...&hl=fi&curr=EUR. None, jos kirjastoa ei ole."""
    try:
        from fast_flights import FlightQuery, Passengers, create_query
    except ImportError:
        return None
    flights = [FlightQuery(date=out, from_airport=o, to_airport=d)]
    if ret:
        flights.append(FlightQuery(date=ret, from_airport=d, to_airport=o))
    q = create_query(flights=flights, trip="round-trip" if ret else "one-way", seat="economy",
                     passengers=Passengers(adults=1), language="fi", currency="EUR")
    return q.params()["tfs"]


DUR_FILTER = "(duration_min IS NULL OR duration_min <= ?)"


def aggregate(conn, max_min):
    """Täyttää fare_min/dest_min uusille keruille. Uusin keruu lasketaan aina uudelleen,
    koska se on voinut olla kesken edellisellä rakennuskerralla. Jos kestoraja (max_min,
    minuutteina) on muuttunut, kaikki keruut lasketaan uudelleen."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'agg_max_duration_min'").fetchone()
    if (row[0] if row else None) != str(max_min):
        conn.execute("UPDATE runs SET aggregated = 0")
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('agg_max_duration_min', ?)",
                     (str(max_min),))
        conn.commit()
    if conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0:
        # Kerran: keruut, jotka tehtiin ennen runs-taulua
        conn.execute("INSERT OR IGNORE INTO runs(collected_at) SELECT DISTINCT collected_at FROM fares")
        conn.commit()
    latest = conn.execute("SELECT MAX(collected_at) FROM runs").fetchone()[0]
    if not latest:
        return None
    todo = [r[0] for r in conn.execute(
        "SELECT collected_at FROM runs WHERE aggregated = 0 OR collected_at = ? ORDER BY collected_at",
        (latest,))]
    for run in todo:
        conn.execute("DELETE FROM fare_min WHERE collected_at = ?", (run,))
        conn.execute("DELETE FROM dest_min WHERE collected_at = ?", (run,))
        # SQLite: MIN()-koosteessa muut sarakkeet tulevat halvimmalta riviltä
        conn.execute(
            "INSERT INTO fare_min(collected_at,origin,destination,outbound_date,return_date,"
            "trip_len,price,airlines,stops) "
            "SELECT collected_at, origin, destination, outbound_date, return_date, "
            "CAST(ROUND(julianday(return_date) - julianday(outbound_date)) AS INTEGER), "
            "MIN(price), airlines, stops FROM fares WHERE collected_at = ? AND " + DUR_FILTER +
            " GROUP BY origin, destination, outbound_date, return_date",
            (run, max_min))
        conn.execute(
            "INSERT INTO dest_min(collected_at,origin,destination,trip_len,price,outbound_date,airlines) "
            "SELECT collected_at, origin, destination, trip_len, MIN(price), outbound_date, airlines "
            "FROM fare_min WHERE collected_at = ? GROUP BY origin, destination, trip_len",
            (run,))
        conn.execute("UPDATE runs SET aggregated = 1 WHERE collected_at = ?", (run,))
        conn.commit()
        print(f"Tiivistetty keruu {run}")
    return latest


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def build(conn, out, max_min, active=None):
    """active: sallitut reitit {"HEL-BKK", ...} (configin nykyiset kohteet); None = kaikki."""
    latest = aggregate(conn, max_min)
    if os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(os.path.join(out, "data"))
    shutil.copy(os.path.join(HERE, "web", "index.html"), os.path.join(out, "index.html"))
    with open(os.path.join(out, ".nojekyll"), "w"):
        pass

    if not latest:
        write_json(os.path.join(out, "data", "overview.json"),
                   {"built": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                    "latest": None, "routes": {}})
        print("Ei dataa vielä - tyhjä sivu rakennettu.")
        return

    runs = [r[0] for r in conn.execute("SELECT collected_at FROM runs ORDER BY collected_at")]
    hist_from = runs[-HISTORY_RUNS] if len(runs) >= HISTORY_RUNS else runs[0]

    # Yleiskuva: halvin per kohde ja matkan pituus jokaisesta keruusta (pieni taulu)
    routes = {}
    for run, o, d, n, price, out_date, airlines in conn.execute(
            "SELECT collected_at, origin, destination, trip_len, price, outbound_date, airlines "
            "FROM dest_min ORDER BY collected_at"):
        if active is not None and f"{o}-{d}" not in active:
            continue  # kohde poistettu seurannasta (data säilyy kannassa)
        r = routes.setdefault(f"{o}-{d}", {"trend": {}, "now": {}})
        k = trip_key(n)
        r["trend"].setdefault(k, []).append([run, price])
        if run == latest:
            ret = ret_date(out_date, n)
            r["now"][k] = {"price": price, "out": out_date, "ret": ret, "airlines": airlines,
                           "tfs": gf_tfs(o, d, out_date, ret)}

    for route in sorted(routes):
        o, d = route.split("-", 1)
        # Uusimman keruun kaikki tallennetut vaihtoehdot
        rows = [list(r) for r in conn.execute(
            "SELECT outbound_date, return_date, price, airlines, stops, duration_min, flight_numbers "
            "FROM fares WHERE collected_at = ? AND origin = ? AND destination = ? AND " + DUR_FILTER +
            " "
            "ORDER BY outbound_date, return_date, price",
            (latest, o, d, max_min))]
        links = {}
        for row in rows:
            key = f"{row[0]}|{row[1] or ''}"
            if key not in links:
                links[key] = gf_tfs(o, d, row[0], row[1])
        write_json(os.path.join(out, "data", "latest", f"{route}.json"),
                   {"run": latest, "rows": rows, "links": links})
        # Lähtöpäiväkohtainen historia viimeisistä keruista
        hist = {}
        for run, out_date, n, price in conn.execute(
                "SELECT collected_at, outbound_date, trip_len, price FROM fare_min "
                "WHERE origin = ? AND destination = ? AND collected_at >= ? ORDER BY collected_at",
                (o, d, hist_from)):
            hist.setdefault(trip_key(n), {}).setdefault(out_date, []).append([run, price])
        write_json(os.path.join(out, "data", "hist", f"{route}.json"), hist)

    write_json(os.path.join(out, "data", "overview.json"), {
        "built": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "latest": latest,
        "n_runs": len(runs),
        "max_hours": round(max_min / 60) if max_min < 10 ** 8 else None,
        "routes": routes,
    })
    print(f"Sivu rakennettu: {out} ({len(routes)} reittiä, {len(runs)} keruuta, uusin {latest})")


def main():
    ap = argparse.ArgumentParser(description="Rakenna hintasivu.")
    ap.add_argument("--db", default="flights.db")
    ap.add_argument("--out", default="_site")
    ap.add_argument("--config", default="config.json")
    args = ap.parse_args()
    try:
        conn = init_db(args.db)
    except RuntimeError as e:
        sys.exit(f"Kantaan ei saatu yhteyttä: {e}")
    cfg = load_config(args.config)
    hours = cfg.get("max_duration_hours") or 0
    active = {f"{o}-{d}" for o in cfg["origins"] for d in cfg["destinations"] if o != d}
    # 0 = ei rajaa: käytetään niin suurta rajaa, ettei mikään lento ylitä sitä
    build(conn, args.out, int(float(hours) * 60) if hours else 10 ** 9, active)


if __name__ == "__main__":
    main()
