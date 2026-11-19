#!/usr/bin/env python3
"""
flightwatch - kerää päivittäin lentohintoja Google Flightsista omaan tietokantaan.

Idea: kun dataa on kertynyt kuukausia, voit analysoida kampanjoiden ajoitusta,
hintatasoja ja sesonkeja. Data on sinun omaasi - sitä ei voi ostaa jälkikäteen.

Datalähteet (--provider):
  gflights (oletus)  suora Google Flights -haku, ilmainen.
                     pip install -r requirements.txt  (fast-flights + typing_extensions)
                     EU:n suostumussivu ohitetaan suostumusevästeellä.
  serpapi            maksullinen SerpApi, ympäristömuuttuja SERPAPI_KEY.
  mock               tekaistu data testaukseen.

Tietokanta:
  Oletuksena paikallinen SQLite (--db flights.db). Jos ympäristömuuttujat
  TURSO_DATABASE_URL ja TURSO_AUTH_TOKEN on asetettu, käytetään Turso-pilvikantaa.

Hälytykset (keruu rikki): kirjataan alerts.log-tiedostoon ja lähetetään sähköpostina,
jos SMTP on asetettu: SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASS, ALERT_TO.
Ilman ALERT_TO:ta sähköposti menee osoitteeseen SMTP_USER.

Testaus:
  python3 flightwatch.py --provider mock --max-calls 5     # putki toimii (tekaistu data)
  python3 flightwatch.py --db-check                        # kantayhteys toimii
  python3 flightwatch.py --test-route HEL-BKK              # oikea Google-haku, yksi reitti
  python3 flightwatch.py --test-alert                      # sähköposti saapuu perille

Käyttö:
  python3 flightwatch.py --max-calls 0                     # kaikki haut
  python3 flightwatch.py --shard 1/6 --max-calls 0         # kuudesosa hauista (rinnakkaisajo)
  python3 flightwatch.py --summary                         # halvimmat tuoreimmasta ajosta
  python3 flightwatch.py --export-csv fares.csv            # kaikki data CSV:nä
  python3 flightwatch.py --dry-run                         # näytä montako hakua tulisi

Asetukset: config.json (sama kansio) tai --config.
  "day_offsets": "1-120"     lähtöpäivät (päiviä tästä päivästä; myös ["1-60", "61-120/3"])
  "trip_lengths": [10, 14]   menopaluun pituudet; tyhjä = yhdensuuntaiset
  "keep_per_search": 5       tallennetaan vain N halvinta per haku (0 = kaikki)
  "max_duration_hours": 24   hylätään lennot, joiden menomatka kestää yli 24 h (0 = ei rajaa)
Menopaluussa hinta on koko matkan hinta ja tallennetut lennot ovat menomatkan lennot.
Kannan return_date on NULL yhdensuuntaisille riveille.
"""
import argparse
import base64
import csv
import datetime as dt
import hashlib
import json
import os
import random
import smtplib
import sqlite3
import ssl
import sys
import time
import traceback
from email.message import EmailMessage
import urllib.error
import urllib.parse
import urllib.request

# Suomen kentät, joilta on (tai on ollut) säännöllistä reittiliikennettä.
FINNISH_AIRPORTS = [
    "HEL", "TMP", "TKU", "OUL", "RVN", "VAA", "KUO", "JYV", "KAJ", "KEM",
    "KTT", "IVL", "KOK", "JOE", "LPP", "POR", "SVL", "MHQ", "KAO", "ENF",
]

DEFAULT_CONFIG = {
    # Lähtökentät. "all" = kaikki FINNISH_AIRPORTS (kallis!).
    "origins": ["HEL", "TKU", "TMP", "OUL", "RVN", "VAA"],
    # Kohteet: suosittuja lomakohteita ja solmukenttiä. Muokkaa vapaasti.
    "destinations": [
        "ARN", "CPH", "OSL", "RIX", "TLL", "LHR", "AMS", "FRA", "BCN", "AGP",
        "PMI", "ATH", "HER", "TFS", "LPA", "FAO", "DXB", "BKK", "JFK", "NRT",
    ],
    # Kuinka monta päivää tulevaisuuteen lähtöpäivä sijoittuu (näytteet).
    "day_offsets": [21, 45, 90, 150],
    # Menopaluun matkan pituudet päivinä, esim. [10, 14]. Tyhjä lista = yhdensuuntaiset.
    "trip_lengths": [],
    # Tallennetaan vain N halvinta vaihtoehtoa per haku (säästää tilaa). 0 = kaikki.
    "keep_per_search": 0,
    # Hylätään lennot, joiden (meno)matkan kesto ylittää tämän (tuntia). 0 = ei rajaa.
    "max_duration_hours": 0,
    "currency": "EUR",
    "language": "fi",
    "sleep_seconds": 1.0,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS fares (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_at TEXT NOT NULL,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    outbound_date TEXT NOT NULL,
    airlines TEXT,
    flight_numbers TEXT,
    stops INTEGER,
    duration_min INTEGER,
    price REAL NOT NULL,
    currency TEXT,
    bucket TEXT,
    provider TEXT
);

CREATE TABLE IF NOT EXISTS insights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_at TEXT NOT NULL,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    outbound_date TEXT NOT NULL,
    lowest_price REAL,
    price_level TEXT,
    typical_low REAL,
    typical_high REAL,
    currency TEXT
);

-- Keruukerrat (sivun rakennus käyttää tätä, ettei koko fares-taulua tarvitse lukea)
CREATE TABLE IF NOT EXISTS runs (
    collected_at TEXT PRIMARY KEY,
    aggregated INTEGER NOT NULL DEFAULT 0
);

-- Halvin hinta per haku ja keruukerta (site_build.py täyttää)
CREATE TABLE IF NOT EXISTS fare_min (
    collected_at TEXT NOT NULL,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    outbound_date TEXT NOT NULL,
    return_date TEXT,
    trip_len INTEGER,
    price REAL NOT NULL,
    airlines TEXT,
    stops INTEGER
);
CREATE INDEX IF NOT EXISTS ix_fare_min_route ON fare_min(origin, destination, collected_at);
CREATE INDEX IF NOT EXISTS ix_fare_min_run ON fare_min(collected_at);

-- Halvin hinta per kohde, matkan pituus ja keruukerta (site_build.py täyttää)
CREATE TABLE IF NOT EXISTS dest_min (
    collected_at TEXT NOT NULL,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    trip_len INTEGER,
    price REAL NOT NULL,
    outbound_date TEXT,
    airlines TEXT
);
CREATE INDEX IF NOT EXISTS ix_dest_min_run ON dest_min(collected_at);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_at TEXT NOT NULL,
    origin TEXT, destination TEXT, outbound_date TEXT,
    message TEXT
);
"""


# --------------------------------------------------------------------------- #
# Datalähteet
# --------------------------------------------------------------------------- #
class FatalProviderError(Exception):
    """Keruu pitää keskeyttää (väärä avain, kiintiö täynnä)."""


class RateLimitedError(RuntimeError):
    """Lähde rajoittaa tai estää hakuja (yhteys katkaistu, virhevastaus, CAPTCHA)."""


class SerpApiProvider:
    name = "serpapi-google-flights"
    URL = "https://serpapi.com/search.json"

    def __init__(self, api_key, currency, language):
        self.api_key = api_key
        self.currency = currency
        self.language = language

    def fetch(self, origin, dest, date, return_date=None):
        params = {
            "engine": "google_flights",
            "departure_id": origin,
            "arrival_id": dest,
            "outbound_date": date,
            "type": "2",  # 2 = yhdensuuntainen, ei vaadi paluupäivää
            "currency": self.currency,
            "hl": self.language,
            "api_key": self.api_key,
        }
        if return_date:
            params["type"] = "1"  # 1 = menopaluu
            params["return_date"] = return_date
        url = self.URL + "?" + urllib.parse.urlencode(params)
        for attempt in (1, 2):
            try:
                with urllib.request.urlopen(url, timeout=60) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")[:300]
                if e.code in (401, 403):
                    raise FatalProviderError(f"HTTP {e.code}: tarkista API-avain. {body}")
                if e.code == 429 and attempt == 1:
                    time.sleep(15)
                    continue
                if e.code >= 500 and attempt == 1:
                    time.sleep(5)
                    continue
                # SerpApi palauttaa 4xx myös "ei tuloksia" -tapauksissa
                try:
                    return json.loads(body)
                except ValueError:
                    raise RuntimeError(f"HTTP {e.code}: {body}")
            except urllib.error.URLError as e:
                if attempt == 1:
                    time.sleep(5)
                    continue
                raise RuntimeError(f"Verkkovirhe: {e}")


class GoogleFlightsProvider:
    """Suora Google Flights -haku (ei API-avainta, ei maksua).

    Asennus: pip install fast-flights typing_extensions
    (fast-flights 3.1.0 tarvitsee typing_extensions-paketin, mutta ei asenna sitä itse.)
    Kirjastosta käytetään vain kyselyn muodostusta (create_query) ja HTTP-asiakasta (primp).
    Sivu haetaan suostumusevästeen kanssa (EU:ssa Google näyttää muuten suostumussivun),
    ja tulokset jäsennetään itse, koska kirjaston parseri kaatuu koko hakuun, jos yhdeltäkin
    tarjoukselta puuttuu hinta. Rakenne on epävirallinen ja voi hajota Googlen muutoksiin -
    siksi terveystarkastus ja sähköposti-ilmoitus (ks. health_check).
    """

    name = "google-flights-direct"
    URL = "https://www.google.com/travel/flights"
    # "Hyväksy kaikki" -suostumus. Jos Google muuttaa muotoa, haku osuu taas suostumussivulle
    # ja ajo keskeytyy selkeään virheeseen.
    CONSENT_COOKIE = "SOCS=CAESEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg; CONSENT=YES+cb"

    def __init__(self, currency):
        try:
            from fast_flights import FlightQuery, Passengers, create_query
            from primp import Client
            from selectolax.lexbor import LexborHTMLParser
        except ImportError as e:
            raise FatalProviderError(
                f"fast-flights-kirjaston tuonti epäonnistui: {e}\n"
                f"Tämä Python: {sys.executable} ({sys.version.split()[0]})\n"
                "Ratkaisu: asenna täsmälleen tälle Pythonille uusin (3.x) versio:\n"
                f'  "{sys.executable}" -m pip install -U --pre fast-flights typing_extensions\n'
                "(Vanha 2.x-versio ei sisällä tarvittavia osia, ja pelkkä `pip install` "
                "voi asentaa toiselle Pythonille tai vanhan version.)"
            )
        self._FlightQuery = FlightQuery
        self._Passengers = Passengers
        self._create_query = create_query
        self._HTMLParser = LexborHTMLParser
        self._Client = Client
        self._new_client()
        self.currency = currency

    # Kun Google rajoittaa: tauko (s) ja uusi yhteys ennen uutta yritystä. Pidempi tauko ei
    # auttanut (GitHubin koneilla 120 s jälkeinen yritys epäonnistui lähes aina), joten
    # epäonnistunut haku siirretään mieluummin ajon loppuun (ks. collect).
    RETRY_WAITS = (30,)

    def _new_client(self):
        self._client = self._Client(
            impersonate="chrome_145", impersonate_os="macos", referer=True, cookie_store=True
        )

    def fetch(self, origin, dest, date, return_date=None):
        """Menopaluussa Google palauttaa menolennot ja hinnan koko matkalle
        (halvimmalla paluulla kyseisen menolennon kanssa)."""
        flights = [self._FlightQuery(date=date, from_airport=origin, to_airport=dest)]
        if return_date:
            flights.append(self._FlightQuery(date=return_date, from_airport=dest, to_airport=origin))
        query = self._create_query(
            flights=flights,
            trip="round-trip" if return_date else "one-way",
            seat="economy",
            passengers=self._Passengers(adults=1),
            language="en-US",
            currency=self.currency,
        )
        last = None
        for wait in (0,) + self.RETRY_WAITS:
            if wait:
                print(f"  Google rajoittaa ({last}) - tauko {wait} s ja uusi yhteys", file=sys.stderr)
                time.sleep(wait)
                self._new_client()
            try:
                return self._fetch_once(query)
            except RateLimitedError as e:
                last = e
        raise last

    def _fetch_once(self, query):
        try:
            resp = self._client.get(
                self.URL, params=query.params(), headers={"Cookie": self.CONSENT_COOKIE}
            )
            html = resp.text
        except Exception as e:
            # esim. "h2 connection driver error: detected excessive load generating behavior"
            raise RateLimitedError(f"Google-yhteysvirhe: {type(e).__name__}: {e}")
        status = getattr(resp, "status_code", 200)
        if status == 429 or "/sorry/" in html[:5000] or "unusual traffic" in html:
            raise RateLimitedError(f"Google estää haut (HTTP {status} / CAPTCHA)")
        if "consent.google.com" in html[:5000] or "<title>Before you continue</title>" in html:
            # Jokainen haku osuisi samaan seinään, joten ajo keskeytetään heti.
            raise FatalProviderError(
                "Google näyttää evästesuostumussivun, vaikka suostumuseväste lähetettiin "
                "(evästeen muoto on ehkä muuttunut). Käytä --provider serpapi."
            )
        try:
            return self._parse(html)
        except RateLimitedError:
            raise
        except Exception as e:
            raise RuntimeError(f"Googlen sivun jäsennys epäonnistui: {type(e).__name__}: {e}")

    def _parse(self, html):
        """Jäsentää sivun upotetun datan SerpApin muotoon (parse_response lukee sen).

        payload[2][0] = parhaat lennot, payload[3][0] = muut. Tarjous: [lento, [[_, hinta], ...]].
        Lento: [tyyppi, [yhtiöt], [osuudet], ..., [9]=kokonaiskesto]. Osuus: [3]=lähtö,
        [6]=kohde, [11]=kesto, [22]=[yhtiökoodi, numero, _, yhtiö]. Rikkinäiset rivit ohitetaan.
        """
        script = self._HTMLParser(html).css_first(r"script.ds\:1")
        if script is None:
            raise RuntimeError("sivulta ei löytynyt lentodataa (ds:1)")
        text = script.text()
        if "errorHasStatus: true" in text:
            # Google palautti virhevastauksen tulosten sijaan - käytännössä rajoitus
            raise RateLimitedError("Google palautti virhevastauksen (errorHasStatus)")
        # Luetaan vain data:-kentän JSON-arvo; perässä voi olla muita kenttiä
        start = text.index("data:") + len("data:")
        while text[start] in " \n\t":
            start += 1
        payload, _ = json.JSONDecoder().raw_decode(text, start)

        def at(seq, *idx):
            for i in idx:
                if not isinstance(seq, list) or len(seq) <= i:
                    return None
                seq = seq[i]
            return seq

        out = {"best_flights": [], "other_flights": []}
        for bucket, pidx in (("best_flights", 2), ("other_flights", 3)):
            for offer in at(payload, pidx, 0) or []:
                price = at(offer, 1, 0, 1)
                flight = at(offer, 0)
                if not isinstance(price, (int, float)) or not isinstance(flight, list):
                    continue  # esim. yhtiö ei näytä hintaa Googlessa
                legs, total = [], 0
                for leg in at(flight, 2) or []:
                    code = at(leg, 22, 0)
                    num = at(leg, 22, 1)
                    dur = at(leg, 11)
                    if isinstance(dur, (int, float)):
                        total += int(dur)
                    legs.append({
                        "airline": at(leg, 22, 3),
                        "flight_number": f"{code} {num}" if code and num else None,
                    })
                if not legs:
                    continue
                duration = at(flight, 9)
                out[bucket].append({
                    "flights": legs,
                    "airlines": [a for a in (at(flight, 1) or []) if isinstance(a, str)],
                    "total_duration": duration if isinstance(duration, int) else (total or None),
                    "price": price,
                })
        return out


class MockProvider:
    """Tekaistu data, jolla voi testata koko putken ilman avainta."""

    name = "mock"

    def fetch(self, origin, dest, date, return_date=None):
        seed = int(hashlib.md5(f"{origin}{dest}{date}{return_date}".encode()).hexdigest()[:8], 16)
        rnd = random.Random(seed + int(time.time()) // 86400)
        base = rnd.randint(40, 400)

        def option(airline, num, stops, price):
            legs = [{"airline": airline, "flight_number": f"{num}"}]
            legs += [{"airline": airline, "flight_number": f"{num}1"}] * stops
            return {"flights": legs, "total_duration": 120 + stops * 90, "price": price}

        return {
            "best_flights": [option("Finnair", "AY 101", 0, base + rnd.randint(0, 60))],
            "other_flights": [
                option("Norwegian", "DY 202", 0, base + rnd.randint(-20, 40)),
                option("SAS", "SK 303", 1, base + rnd.randint(0, 80)),
            ],
            "price_insights": {
                "lowest_price": base,
                "price_level": rnd.choice(["low", "typical", "high"]),
                "typical_price_range": [base + 10, base + 90],
            },
        }


# --------------------------------------------------------------------------- #
# Jäsennys
# --------------------------------------------------------------------------- #
def parse_response(data):
    """Palauttaa (fares, insight). Tyhjä lista, jos reitillä ei ole tuloksia."""
    fares = []
    for bucket in ("best_flights", "other_flights"):
        for item in data.get(bucket) or []:
            legs = item.get("flights") or []
            price = item.get("price")
            if price is None or not legs:
                continue
            airlines = [a for a in (item.get("airlines") or []) if a]
            numbers = []
            for leg in legs:
                a = leg.get("airline")
                if a and a not in airlines:
                    airlines.append(a)
                n = leg.get("flight_number")
                if n:
                    numbers.append(str(n))
            fares.append(
                {
                    "airlines": "+".join(airlines),
                    "flight_numbers": ",".join(numbers),
                    "stops": max(len(legs) - 1, 0),
                    "duration_min": item.get("total_duration"),
                    "price": float(price),
                    "bucket": "best" if bucket == "best_flights" else "other",
                }
            )
    insight = None
    pi = data.get("price_insights")
    if pi:
        rng = pi.get("typical_price_range") or [None, None]
        insight = {
            "lowest_price": pi.get("lowest_price"),
            "price_level": pi.get("price_level"),
            "typical_low": rng[0] if len(rng) > 0 else None,
            "typical_high": rng[1] if len(rng) > 1 else None,
        }
    return fares, insight


# --------------------------------------------------------------------------- #
# Keruu
# --------------------------------------------------------------------------- #
def filter_duration(fares, max_hours):
    """Poistaa liian pitkät lennot. Tuntematon kesto säilytetään."""
    if not max_hours:
        return fares
    limit = float(max_hours) * 60
    return [f for f in fares if not f.get("duration_min") or f["duration_min"] <= limit]


def load_config(path):
    cfg = dict(DEFAULT_CONFIG)
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    if cfg["origins"] == "all" or cfg["origins"] == ["all"]:
        cfg["origins"] = list(FINNISH_AIRPORTS)
    cfg["day_offsets"] = parse_day_offsets(cfg["day_offsets"])
    cfg["trip_lengths"] = [int(x) for x in (cfg.get("trip_lengths") or [])]
    return cfg


def parse_day_offsets(value):
    """Hyväksyy listan [21, 45], välin "1-120" tai sekoituksen ["1-60", 90, "120-180/7"].

    Valinnainen /N on askel: "120-180/7" = joka 7. päivä.
    """
    items = value if isinstance(value, list) else [value]
    days = set()
    for it in items:
        if isinstance(it, int):
            days.add(it)
            continue
        s = str(it).strip()
        step = 1
        if "/" in s:
            s, step_s = s.split("/", 1)
            step = int(step_s)
        if "-" in s:
            a, b = (int(x) for x in s.split("-", 1))
            days.update(range(a, b + 1, step))
        else:
            days.add(int(s))
    return sorted(days)


def build_jobs(cfg, today):
    jobs = []
    for origin in cfg["origins"]:
        for dest in cfg["destinations"]:
            if origin == dest:
                continue
            for off in cfg["day_offsets"]:
                out = today + dt.timedelta(days=off)
                if not cfg["trip_lengths"]:
                    jobs.append((origin, dest, out.isoformat(), None))
                for n in cfg["trip_lengths"]:
                    ret = out + dt.timedelta(days=n)
                    jobs.append((origin, dest, out.isoformat(), ret.isoformat()))
    # Pyöritetään järjestystä päivittäin, jotta rajatulla budjetilla kaikki
    # reitit tulevat ajan mittaan katetuksi.
    random.Random(today.isoformat()).shuffle(jobs)
    return jobs


class _Cursor:
    def __init__(self, cols=(), rows=()):
        self.description = [(c,) + (None,) * 6 for c in cols] if cols else None
        self._rows = list(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


class TursoConnection:
    """Pieni sqlite3-yhteensopiva kääre Turson HTTP-rajapinnalle (Hrana, /v2/pipeline).

    Ei vaadi lisäkirjastoja. Kirjoitukset puskuroidaan ja lähetetään commit():ssa yhdellä
    pyynnöllä yhtenä transaktiona; lukukysely lähettää ensin puskuroidut kirjoitukset.
    """

    WRITE_WORDS = ("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP", "REPLACE")

    def __init__(self, url, token):
        if url.startswith("libsql://"):
            url = "https://" + url[len("libsql://"):]
        self.endpoint = url.rstrip("/") + "/v2/pipeline"
        self.token = token
        self._pending = []

    @staticmethod
    def _arg(v):
        if v is None:
            return {"type": "null"}
        if isinstance(v, bool):
            v = int(v)
        if isinstance(v, int):
            return {"type": "integer", "value": str(v)}
        if isinstance(v, float):
            return {"type": "float", "value": v}
        return {"type": "text", "value": str(v)}

    @staticmethod
    def _value(c):
        t = c.get("type")
        if t == "null":
            return None
        if t == "integer":
            return int(c["value"])
        if t == "float":
            return float(c["value"])
        if t == "blob":
            return base64.b64decode(c.get("base64", ""))
        return c.get("value")

    def _send(self, stmts):
        reqs = [{"type": "execute", "stmt": {"sql": sql, "args": [self._arg(a) for a in params]}}
                for sql, params in stmts]
        reqs.append({"type": "close"})
        req = urllib.request.Request(
            self.endpoint,
            data=json.dumps({"requests": reqs}).encode(),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        for attempt in range(1, 4):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.load(resp)
                break
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")[:300]
                if e.code < 500 or attempt == 3:
                    raise RuntimeError(f"Turso HTTP {e.code}: {body}")
            except urllib.error.URLError as e:
                if attempt == 3:
                    raise RuntimeError(f"Turso-yhteysvirhe: {e}")
            time.sleep(3 * attempt)
        last = None
        for res in data.get("results", []):
            if res.get("type") == "error":
                raise RuntimeError(f"Turso SQL-virhe: {res.get('error', {}).get('message')}")
            r = (res.get("response") or {}).get("result")
            if r is not None:
                last = r
        if not last:
            return _Cursor()
        cols = [c.get("name") for c in last.get("cols", [])]
        rows = [tuple(self._value(c) for c in row) for row in last.get("rows", [])]
        return _Cursor(cols, rows)

    def execute(self, sql, params=()):
        if sql.lstrip().split(None, 1)[0].upper() in self.WRITE_WORDS:
            self._pending.append((sql, tuple(params)))
            return _Cursor()
        self.commit()
        return self._send([(sql, tuple(params))])

    def executescript(self, script):
        for stmt in script.split(";"):
            if stmt.strip():
                self._pending.append((stmt.strip(), ()))
        self.commit()

    def commit(self):
        if self._pending:
            stmts, self._pending = self._pending, []
            self._send([("BEGIN", ())] + stmts + [("COMMIT", ())])

    def close(self):
        self.commit()


def init_db(path):
    url, token = os.environ.get("TURSO_DATABASE_URL"), os.environ.get("TURSO_AUTH_TOKEN")
    if url:
        if not token:
            sys.exit("TURSO_DATABASE_URL on asetettu, mutta TURSO_AUTH_TOKEN puuttuu.")
        conn = TursoConnection(url, token)
    else:
        conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    # return_date: paluupäivä menopaluulle, NULL = yhdensuuntainen.
    # Lisätään myös vanhoihin kantoihin (ALTER TABLE säilyttää olemassa olevat rivit).
    for table in ("fares", "insights", "errors"):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if "return_date" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN return_date TEXT")
    conn.execute("DROP INDEX IF EXISTS ix_fares_route")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_fares_route2 ON fares"
        "(origin, destination, outbound_date, return_date, collected_at)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_fares_collected ON fares(collected_at)")
    conn.commit()
    return conn


MAX_CONSECUTIVE_FAILURES = 15  # näin monta peräkkäistä virhettä -> kone on estetty, lopetetaan


def collect(conn, provider, cfg, max_calls, dry_run, shard=(1, 1), collected_at=None,
            max_minutes=None):
    """shard=(k, n): ajetaan vain k:s n:stä osasta. collected_at: yhteinen aikaleima, jotta
    rinnakkaiset osat näkyvät samana keruukertana (ja laskevat päivät samasta päivästä)."""
    collected_at = collected_at or dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    today = dt.datetime.fromisoformat(collected_at).date()
    jobs = build_jobs(cfg, today)  # sama järjestys kaikissa osissa (siemen = päivä)
    total = len(jobs)
    k, n = shard
    jobs = jobs[k - 1::n]
    jobs = jobs[:max_calls] if max_calls else jobs
    part = f" (osa {k}/{n})" if n > 1 else ""
    print(f"Hakuja yhteensä mahdollista: {total}, tällä ajolla{part}: {len(jobs)}")
    stats = {"attempted": 0, "with_fares": 0, "empty": 0, "failed": 0,
             "n_fares": 0, "no_airline": 0, "aborted": False, "abort_reason": "",
             "last_error": "", "planned": len(jobs), "timed_out": False, "rate_limited": 0,
             "rate_limit_hits": 0, "blocked": ""}
    if dry_run:
        return stats
    conn.execute("INSERT OR IGNORE INTO runs(collected_at) VALUES (?)", (collected_at,))
    conn.commit()
    ok = empty = failed = 0
    consecutive = 0
    started = time.monotonic()
    # Googlen rajoittama haku yritetään uudelleen kerran ajon lopussa (rajoitus on hetkellinen)
    queue = [(job, False) for job in jobs]
    for i, ((origin, dest, date, ret), deferred) in enumerate(queue, 1):
        if max_minutes and time.monotonic() - started > max_minutes * 60:
            print(f"Aikaraja {max_minutes} min täynnä - lopetetaan, "
                  f"{stats['attempted']}/{len(jobs)} hakua tehty.")
            stats["timed_out"] = True
            break
        if consecutive >= MAX_CONSECUTIVE_FAILURES:
            msg = (f"{consecutive} peräkkäistä hakua epäonnistui - Google estää todennäköisesti "
                   f"tämän koneen haut. Viimeisin: {stats['last_error']}")
            print(f"KESKEYTETTY: {msg}", file=sys.stderr)
            stats["blocked"] = msg  # ongelma vain, jos kattavuus jää heikoksi (health_check)
            break
        if not deferred:
            stats["attempted"] += 1
        try:
            data = provider.fetch(origin, dest, date, ret)
        except FatalProviderError as e:
            print(f"KESKEYTETTY: {e}", file=sys.stderr)
            stats["aborted"], stats["abort_reason"] = True, str(e)
            break
        except RateLimitedError as e:
            stats["rate_limit_hits"] += 1
            consecutive += 1
            stats["last_error"] = str(e)[:300]
            if not deferred:
                queue.append(((origin, dest, date, ret), True))
                continue
            stats["rate_limited"] += 1
            failed += 1
            conn.execute(
                "INSERT INTO errors(collected_at,origin,destination,outbound_date,return_date,message) "
                "VALUES (?,?,?,?,?,?)",
                (collected_at, origin, dest, date, ret, str(e)[:500]),
            )
            conn.commit()
            continue
        except Exception as e:  # yksittäinen virhe ei kaada koko ajoa
            failed += 1
            consecutive += 1
            stats["last_error"] = str(e)[:300]
            conn.execute(
                "INSERT INTO errors(collected_at,origin,destination,outbound_date,return_date,message) "
                "VALUES (?,?,?,?,?,?)",
                (collected_at, origin, dest, date, ret, str(e)[:500]),
            )
            conn.commit()
            continue

        if isinstance(data, dict) and data.get("error") and not (
            data.get("best_flights") or data.get("other_flights")
        ):
            msg = str(data["error"])
            if "run out of searches" in msg.lower() or "invalid api key" in msg.lower():
                print(f"KESKEYTETTY: {msg}", file=sys.stderr)
                stats["aborted"], stats["abort_reason"] = True, msg
                break
            empty += 1
            continue

        consecutive = 0
        fares, insight = parse_response(data)
        fares = filter_duration(fares, cfg.get("max_duration_hours"))
        if cfg.get("keep_per_search"):
            fares = sorted(fares, key=lambda f: f["price"])[: int(cfg["keep_per_search"])]
        stats["n_fares"] += len(fares)
        stats["no_airline"] += sum(1 for f in fares if not f["airlines"])
        if not fares:
            empty += 1
        for f in fares:
            conn.execute(
                "INSERT INTO fares(collected_at,origin,destination,outbound_date,return_date,airlines,"
                "flight_numbers,stops,duration_min,price,currency,bucket,provider) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (collected_at, origin, dest, date, ret, f["airlines"], f["flight_numbers"],
                 f["stops"], f["duration_min"], f["price"], cfg["currency"], f["bucket"],
                 provider.name),
            )
        if insight:
            conn.execute(
                "INSERT INTO insights(collected_at,origin,destination,outbound_date,return_date,"
                "lowest_price,price_level,typical_low,typical_high,currency) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (collected_at, origin, dest, date, ret, insight["lowest_price"], insight["price_level"],
                 insight["typical_low"], insight["typical_high"], cfg["currency"]),
            )
        conn.commit()
        if fares:
            ok += 1
        if i % 20 == 0:
            print(f"  {i}/{len(queue)} ...")
        if provider.name != "mock":
            # Satunnainen viive: ei hakuja täsmälleen samassa tahdissa (Google rajoittaa helposti)
            time.sleep(cfg["sleep_seconds"] * random.uniform(0.7, 1.8))
    stats["with_fares"], stats["empty"], stats["failed"] = ok, empty, failed
    print(f"Valmis. Tuloksia: {ok}, tyhjiä: {empty}, virheitä: {failed} "
          f"(joista Googlen rajoittamia {stats['rate_limited']})")
    return stats


# --------------------------------------------------------------------------- #
# Terveystarkastus ja sähköposti-ilmoitus
# --------------------------------------------------------------------------- #
ALERT_THROTTLE_HOURS = 20  # enintään yksi hälytys vuorokaudessa, ettei postilaatikko täyty


def send_alert(subject, body, conn=None, force=False):
    """Kirjaa hälytyksen alerts.log:iin ja lähettää sen sähköpostitse, jos SMTP on asetettu.

    Ympäristömuuttujat: SMTP_HOST, SMTP_PORT (oletus 587), SMTP_USER, SMTP_PASS,
    ALERT_TO (oletus alla). Palauttaa True, jos sähköposti lähti.
    """
    to = os.environ.get("ALERT_TO") or os.environ.get("SMTP_USER", "")
    now = dt.datetime.now(dt.timezone.utc)
    with open("alerts.log", "a", encoding="utf-8") as f:
        f.write(f"[{now.isoformat(timespec='seconds')}] {subject}\n{body}\n\n")

    if conn is not None and not force:
        row = conn.execute("SELECT value FROM meta WHERE key='last_alert_at'").fetchone()
        if row:
            last = dt.datetime.fromisoformat(row[0])
            if now - last < dt.timedelta(hours=ALERT_THROTTLE_HOURS):
                print("Hälytys kirjattu, mutta sähköposti vaimennettu (lähetetty alle vuorokausi sitten).")
                return False

    host, user, pw = (os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS"))
    if not (host and user and pw):
        print("VAROITUS: SMTP_HOST/SMTP_USER/SMTP_PASS puuttuu - sähköpostia ei lähetetty "
              "(hälytys on tiedostossa alerts.log).", file=sys.stderr)
        return False

    msg = EmailMessage()
    msg["Subject"] = "[flightwatch] " + subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(body)
    port = int(os.environ.get("SMTP_PORT") or 587)  # tyhjä GitHub-secret -> oletus
    ctx = ssl.create_default_context()
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
                s.login(user, pw)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.starttls(context=ctx)
                s.login(user, pw)
                s.send_message(msg)
    except Exception as e:
        print(f"Sähköpostin lähetys epäonnistui: {type(e).__name__}: {e}", file=sys.stderr)
        return False
    if conn is not None:
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('last_alert_at', ?)", (now.isoformat(),))
        conn.commit()
    print(f"Hälytys lähetetty: {to}")
    return True


def health_check(stats, provider_name):
    """Palauttaa listan ongelmista (tyhjä = kaikki hyvin)."""
    problems = []
    if stats["attempted"] == 0:
        return problems
    if stats["aborted"]:
        problems.append(f"Keruu keskeytyi: {stats['abort_reason']}")
    # Osa reiteistä on aidosti tyhjiä (pienet kentät, liian kaukaiset päivät), joten raja on matala.
    # Sivurakenteen hajoaminen näkyy virheinä (failed), ei tyhjinä tuloksina.
    ratio = stats["with_fares"] / stats["attempted"]
    if ratio < 0.15 and stats["attempted"] >= 10 or stats["with_fares"] == 0:
        problems.append(
            f"Vain {stats['with_fares']}/{stats['attempted']} hausta ({ratio:.0%}) palautti hintoja. "
            "Google on voinut muuttaa sivuaan, tai suostumusseinä/rajoitus estää haut."
        )
    # Muut kuin rajoitusvirheet viittaavat rikkinäiseen jäsennykseen tai muuhun vikaan
    other_failed = stats["failed"] - stats.get("rate_limited", 0)
    if other_failed > 0.3 * stats["attempted"]:
        problems.append(
            f"{other_failed} hakua päättyi virheeseen. Viimeisin: {stats['last_error']}"
        )
    # Googlen rajoitukset ovat GitHubin koneilla tavallisia; ongelma vasta, jos kattavuus kärsii
    done = stats["with_fares"] + stats["empty"]
    planned = stats.get("planned", 0)
    if planned and done < 0.5 * planned:
        problems.append(
            f"Vain {done}/{planned} suunnitellusta hausta saatiin tehtyä "
            f"(Google rajoitti {stats.get('rate_limit_hits', 0)} kertaa"
            + (", aikaraja täyttyi" if stats.get("timed_out") else "")
            + (f"; {stats['blocked']}" if stats.get("blocked") else "") + ")."
        )
    if stats["n_fares"] and stats["no_airline"] / stats["n_fares"] > 0.5:
        problems.append("Yli puolelta tuloksista puuttuu yhtiön nimi - jäsennys on todennäköisesti rikki.")
    return problems


def test_route(provider, route, trip_length=None):
    """Yksi haku, tulostaa tulokset. Ei kirjoita kantaan. trip_length = menopaluun pituus."""
    try:
        origin, dest = route.upper().split("-")
    except ValueError:
        sys.exit("Anna reitti muodossa HEL-ARN")
    out = dt.date.today() + dt.timedelta(days=30)
    date = out.isoformat()
    ret = (out + dt.timedelta(days=trip_length)).isoformat() if trip_length else None
    print(f"Testihaku {origin}->{dest}, lähtö {date}"
          + (f", paluu {ret} (menopaluun kokonaishinta, lennot = menomatka)" if ret else ", yhdensuuntainen")
          + " ...")
    try:
        data = provider.fetch(origin, dest, date, ret)
    except Exception as e:
        print(f"HAKU EPÄONNISTUI: {e}", file=sys.stderr)
        return False
    fares, insight = parse_response(data)
    if not fares:
        print("EI TULOKSIA. Jos käytät gflights-lähdettä, syynä voi olla suostumusseinä, "
              "rajoitus tai Googlen muutos.")
        return False
    for f in sorted(fares, key=lambda x: x["price"])[:10]:
        print(f"  {f['price']:>7.0f}  {f['airlines'] or '?':<28} vaihtoja: {f['stops']}  {f['flight_numbers']}")
    print(f"Yhteensä {len(fares)} vaihtoehtoa. Vertaa halvinta hintaa Google Flightsiin selaimessa.")
    return True


# --------------------------------------------------------------------------- #
# Raportit
# --------------------------------------------------------------------------- #
def summary(conn):
    row = conn.execute("SELECT MAX(collected_at) FROM fares").fetchone()
    if not row or not row[0]:
        print("Ei dataa vielä.")
        return
    latest = row[0]
    print(f"Tuorein ajo: {latest}\n")
    q = """
    SELECT origin, destination, outbound_date, return_date, MIN(price) AS p,
           (SELECT airlines FROM fares f2
             WHERE f2.collected_at = f.collected_at AND f2.origin = f.origin
               AND f2.destination = f.destination AND f2.outbound_date = f.outbound_date
               AND f2.return_date IS f.return_date
             ORDER BY price LIMIT 1) AS airline
    FROM fares f WHERE collected_at = ?
    GROUP BY origin, destination, outbound_date, return_date
    ORDER BY p LIMIT 25
    """
    print(f"{'Reitti':<10}{'Lähtö':<12}{'Paluu':<12}{'Hinta':>8}  Yhtiö")
    for o, d, date, ret, p, a in conn.execute(q, (latest,)):
        print(f"{o + '-' + d:<10}{date:<12}{ret or '-':<12}{p:>8.0f}  {a}")
    n_fares = conn.execute("SELECT COUNT(*) FROM fares").fetchone()[0]
    n_runs = conn.execute("SELECT COUNT(DISTINCT collected_at) FROM fares").fetchone()[0]
    print(f"\nYhteensä {n_fares} hintariviä, {n_runs} keruukertaa.")


def export_csv(conn, path):
    cur = conn.execute(
        "SELECT collected_at,origin,destination,outbound_date,return_date,airlines,flight_numbers,"
        "stops,duration_min,price,currency,bucket,provider FROM fares ORDER BY collected_at"
    )
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([c[0] for c in cur.description])
        w.writerows(cur)
    print(f"Tallennettu: {path}")


def main():
    ap = argparse.ArgumentParser(description="Kerää lentohintoja Google Flightsista.")
    ap.add_argument("--db", default="flights.db")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--provider", choices=["gflights", "serpapi", "mock"], default="gflights")
    ap.add_argument("--max-calls", type=int, default=100, help="Hakujen enimmäismäärä ajoa kohti (0 = kaikki)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--export-csv")
    ap.add_argument("--test-alert", action="store_true", help="Lähetä testisähköposti")
    ap.add_argument("--test-route", metavar="HEL-ARN", help="Yksi testihaku, ei kirjoita kantaan")
    ap.add_argument("--trip-length", type=int, metavar="PV",
                    help="--test-route: menopaluun pituus päivinä (oletus: configin ensimmäinen)")
    ap.add_argument("--shard", default="1/1", metavar="K/N",
                    help="Aja vain K:s N:stä osasta hakuja (rinnakkaisajoa varten), esim. 2/6")
    ap.add_argument("--collected-at", metavar="ISO-AIKA",
                    help="Yhteinen keruuaikaleima rinnakkaisille osille, esim. 2026-11-15T01:00:00+00:00")
    ap.add_argument("--db-check", action="store_true", help="Testaa kantayhteys ja tulosta rivimäärät")
    ap.add_argument("--max-minutes", type=float,
                    help="Lopeta keruu siististi tämän ajan jälkeen (GitHubin 6 h raja)")
    args = ap.parse_args()
    try:
        k, n = (int(x) for x in args.shard.split("/"))
        assert 1 <= k <= n
    except (ValueError, AssertionError):
        sys.exit("--shard muodossa K/N, esim. 2/6")

    cfg = load_config(args.config)
    try:
        conn = init_db(args.db)
    except RuntimeError as e:
        sys.exit(f"Kantaan ei saatu yhteyttä: {e}\n"
                 "Tarkista TURSO_DATABASE_URL ja TURSO_AUTH_TOKEN (tunnus voi olla vanhentunut).")

    if args.test_alert:
        ok = send_alert("Testihälytys", "Jos saat tämän viestin, hälytykset toimivat.", conn, force=True)
        return sys.exit(0 if ok else 1)
    if args.db_check:
        kind = "Turso" if isinstance(conn, TursoConnection) else f"SQLite ({args.db})"
        for t in ("fares", "insights", "errors"):
            print(f"{t}: {conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]} riviä")
        print(f"Kantayhteys OK: {kind}")
        return
    if args.summary:
        return summary(conn)
    if args.export_csv:
        return export_csv(conn, args.export_csv)

    try:
        if args.provider == "mock":
            provider = MockProvider()
        elif args.provider == "gflights":
            provider = GoogleFlightsProvider(cfg["currency"])
        else:
            key = os.environ.get("SERPAPI_KEY")
            if not key and not args.dry_run:
                sys.exit("Aseta ympäristömuuttuja SERPAPI_KEY (tai käytä --provider mock testiin).")
            provider = SerpApiProvider(key, cfg["currency"], cfg["language"])
    except FatalProviderError as e:
        sys.exit(str(e))

    if args.test_route:
        n = args.trip_length or (cfg["trip_lengths"][0] if cfg["trip_lengths"] else None)
        return sys.exit(0 if test_route(provider, args.test_route, n) else 1)

    try:
        stats = collect(conn, provider, cfg, args.max_calls, args.dry_run,
                        shard=(k, n), collected_at=args.collected_at,
                        max_minutes=args.max_minutes)
    except Exception:
        # Odottamaton kaatuminen: hälytys + virhe näkyviin
        try:
            send_alert("Keruu kaatui", traceback.format_exc(), conn)
        except Exception as e:  # esim. kanta ei vastaa -> ei kaadeta hälytykseen
            print(f"Hälytyksen lähetys epäonnistui: {e}", file=sys.stderr)
        raise
    if args.dry_run:
        return
    problems = health_check(stats, provider.name)
    if problems:
        body = (
            f"Lähde: {provider.name}\nAjo: {dt.datetime.now().isoformat(timespec='seconds')}"
            f" (osa {k}/{n})\n\n"
            + "\n".join("- " + p for p in problems)
            + "\n\nMitä tehdä: aja `python3 flightwatch.py --test-route HEL-ARN` ja katso "
              "tulostuuko hintoja. Jos jäsennys on rikki, Googlen sivun rakenne on muuttunut "
              "(ks. GoogleFlightsProvider._parse). Varalla: --provider serpapi."
        )
        print("ONGELMIA HAVAITTU:\n" + body, file=sys.stderr)
        send_alert("Keruu rikki tai heikkenee", body, conn)
        sys.exit(2)


if __name__ == "__main__":
    main()