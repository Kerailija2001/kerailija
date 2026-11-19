# flightwatch GitHub Actionsiin – asennusohje

Keruu ajetaan joka yö GitHubin palvelimilla, ja hinnat tallentuvat Turso-pilvikantaan.
Omaa konetta ei tarvitse pitää päällä. Kaikki tässä kuvattu on ilmaista.

**Mitä kerätään:** Helsingistä 21 kohteeseen Kaakkois-Aasiassa, Intiassa ja Amerikoissa, menopaluu
10 ja 14 päivän matkoille, jokainen lähtöpäivä 1–120 päivän päähän. Yhteensä 5 040
hakua yössä kuudessa rinnakkaisessa osassa, joista kukin kestää noin tunnin.
Per haku tallennetaan 5 halvinta vaihtoehtoa. Asetukset ovat tiedostossa `config.json`.

---

## 1. Turso-tietokanta

1. Mene osoitteeseen https://turso.tech ja rekisteröidy (onnistuu GitHub-tunnuksilla).
2. Luo uusi tietokanta, esimerkiksi nimellä `flightwatch`. Valitse sijainniksi Eurooppa,
   jos sitä tarjotaan.
3. Kopioi tietokannan **URL**. Se alkaa `libsql://` ja päättyy `.turso.io`.
4. Luo tietokannalle **token** (Create Token). Valitse voimassaoloksi *Never*, jos se on
   tarjolla. Muuten token pitää uusia vanhenemisen jälkeen.
5. Pidä URL ja token tallessa seuraavaa vaihetta varten. Älä laita niitä mihinkään tiedostoon.

## 2. GitHub-repo

1. Luo uusi repo: **New repository**, nimeksi `flightwatch`, näkyvyys **Public**.
   Julkinen repo tarvitaan, koska yksityisen repon ilmaisminuutit (2 000/kk) eivät riitä
   noin 150 tunnin kuukausittaiseen keruuseen. Koodi näkyy muille, mutta tunnukset ja
   data eivät.
2. Lähetä nämä tiedostot repoon (älä lähetä `flights.db`-tiedostoa):
   ```
   flightwatch.py
   config.json
   requirements.txt
   .gitignore
   SETUP.md
   .github/workflows/collect.yml
   .github/workflows/test.yml
   ```
   **Helpoiten komentoriviltä** (vaatii Gitin, https://git-scm.com):
   ```
   cd C:\code\flightwatch
   git init -b main
   git add flightwatch.py config.json requirements.txt .gitignore SETUP.md .github
   git commit -m "flightwatch"
   git remote add origin https://github.com/KÄYTTÄJÄNIMESI/flightwatch.git
   git push -u origin main
   ```
   **Tai selaimessa:** valitse repossa *Add file → Upload files* ja vedä tiedostot sekä
   `.github`-kansio ikkunaan. Tarkista, että tiedostot päätyivät polkuun
   `.github/workflows/`.

## 3. Salaisuudet (Secrets)

Valitse repossa **Settings → Secrets and variables → Actions → New repository secret**
ja lisää:

| Nimi | Arvo |
|---|---|
| `TURSO_DATABASE_URL` | Turson URL (`libsql://...turso.io`) |
| `TURSO_AUTH_TOKEN` | Turson token |

Sähköpostihälytykset ovat vapaaehtoisia (kohta 6):

| Nimi | Arvo |
|---|---|
| `SMTP_HOST` | esim. `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | lähettäjän sähköpostiosoite |
| `SMTP_PASS` | sovellussalasana (ei tavallinen salasana) |
| `ALERT_TO` | osoite, johon hälytykset tulevat |

## 4. Koeajo

1. Avaa repossa **Actions**-välilehti. Jos GitHub kysyy, salli workflowt.
2. Valitse vasemmalta **flightwatch-testi** ja paina **Run workflow**.
3. Avaa ajo, kun se on valmis (noin 5 minuuttia). Ajossa on neljä vaihetta:
   1. **Kantayhteys** – tulostaa `Kantayhteys OK: Turso`.
   2. **Yksi Google-haku** – tulostaa HEL–BKK-hintoja. **Tämä on tärkein vaihe.** Jos tulee
      `EI TULOKSIA` tai virhe, Google estää GitHubin palvelimet, eikä tämä vaihtoehto toimi.
   3. **Koekeruu** – 30 hakua. Rivillä `Tuloksia: X, tyhjiä: Y` X:n pitäisi olla
      selvästi yli puolet.
   4. **Tulokset** – listaa halvimmat löydetyt lennot.

Koekeruun hinnat jäävät kantaan omana keruukertanaan. Ne ovat oikeaa dataa.

## 5. Varsinainen keruu

Kun koeajo onnistuu, mitään ei tarvitse tehdä: **flightwatch-keruu** käynnistyy joka yö
klo 01:00 UTC. Ensimmäisen keruun voi käynnistää heti kohdasta *Actions →
flightwatch-keruu → Run workflow*.

- Keruu ajaa kuusi osaa rinnakkain. Kaikki osat tallentuvat samana keruukertana.
- Erillinen `keepalive`-vaihe estää GitHubia pysäyttämästä ajastusta 60 päivän
  hiljaiselon jälkeen.

## 6. Kun jokin hajoaa

- **GitHub lähettää sähköpostia**, kun ajo epäonnistuu. Viesti menee GitHub-tilisi
  osoitteeseen, eikä tätä tarvitse erikseen asettaa. Flightwatch merkitsee ajon
  epäonnistuneeksi myös silloin, kun haut alkavat palauttaa liian vähän hintoja.
- Jos SMTP-salaisuudet on asetettu, flightwatch lähettää lisäksi oman hälytyksen, jossa on
  ongelman kuvaus. Hälytyksiä tulee enintään yksi vuorokaudessa.
- Yleisin syy on, että Google on muuttanut sivuaan. Silloin korjattava kohta on
  `GoogleFlightsProvider._parse` tiedostossa `flightwatch.py`.

## 7. Hintasivu (selaimessa)

Workflow **flightwatch-sivu** rakentaa selattavan sivun jokaisen keruun jälkeen ja
julkaisee sen GitHub Pagesiin. Pages pitää ottaa käyttöön kerran:

1. Valitse repossa **Settings → Pages → Build and deployment → Source** ja vaihda arvoksi
   **GitHub Actions**.
2. Valitse **Actions → flightwatch-sivu → Run workflow**. Jatkossa sivu päivittyy
   automaattisesti.
3. Sivu löytyy osoitteesta **https://kerailija2001.github.io/kerailija/**

Sivulla on:
- **Yleiskuva:** kunkin kohteen halvin hinta nyt (10 ja 14 pv), lähtöpäivä, muutos
  viikon takaiseen ja hintakehitys kaaviona.
- **Kohdesivu:** halvin hinta jokaiselle lähtöpäivälle pylväinä, joista halvimmat 10 %
  on merkitty vihreällä. Klikkaamalla päivää näet sen 5 halvinta vaihtoehtoa
  (yhtiö, vaihdot, kesto, lennot), linkin Google Flightsiin ja päivän hinnan kehityksen
  aiemmissa keruissa.

Sivu on julkinen kuten repokin, mutta hakukoneita pyydetään olemaan indeksoimatta sitä.
Sivulla näkyy vain hintadataa, ei tunnuksia.

Sivun voi rakentaa myös omalla koneella (Turso-muuttujat asetettuina, ks. kohta 8):

```powershell
python site_build.py --out _site
python -m http.server -d _site 8000     # avaa http://localhost:8000
```

## 8. Datan katselu omalla koneella

Aseta PowerShellissä Turso-tunnukset ja käytä samoja komentoja kuin paikallisesti:

```powershell
$env:TURSO_DATABASE_URL = "libsql://...turso.io"
$env:TURSO_AUTH_TOKEN = "..."
python flightwatch.py --summary                  # halvimmat tuoreimmasta keruusta
python flightwatch.py --db-check                 # rivimäärät
python flightwatch.py --export-csv fares.csv     # kaikki data Exceliin
```

Turson verkkosivuilla voi myös ajaa SQL-kyselyitä suoraan. Esimerkiksi halvin
14 päivän matka Bangkokiin jokaiselle lähtöpäivälle uusimmasta keruusta:

```sql
SELECT outbound_date, MIN(price)
FROM fares
WHERE destination = 'BKK'
  AND julianday(return_date) - julianday(outbound_date) = 14
  AND collected_at = (SELECT MAX(collected_at) FROM fares)
GROUP BY outbound_date
ORDER BY outbound_date;
```

Kun dataa on kertynyt pidempään, CSV-viennin sijaan kannattaa käyttää Turson omaa
komentorivityökalua (`turso db shell flightwatch .dump`). Se on nopeampi suurilla
määrillä.

## Rajoista

- **GitHub Actions:** julkisessa repossa minuutteja ei rajoiteta. Yksi ajo saa kestää
  enintään 6 tuntia, minkä takia keruu on jaettu kuuteen osaan.
- **Turso:** ilmaisessa tasossa on rajat tallennustilalle ja kirjoitetuille riveille.
  Keruu kirjoittaa noin 15 000–25 000 riviä yössä. Tarkista ajantasaiset rajat Turson
  hinnastosta. Jos raja tulee vastaan, pienennä `keep_per_search`-arvoa tai harvenna
  kaukaisia lähtöpäiviä (`"day_offsets": ["1-60", "61-120/3"]`).
