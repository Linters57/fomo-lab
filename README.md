# Fomo Lab — paper trading

Onderzoeksprototype met **100 virtuele USDC**. Geen wallet, private key,
ondertekende transactie, echte order of officiële FOMO-accountkoppeling.
De strategie heeft geen aangetoonde winstgevendheid. Python 3.11+; alleen
de standaardbibliotheek. Handelsmotor 0.1.1, gedeelde hosting toegevoegd op
6 oktober 2026.

## Lokaal gebruiken

```sh
python3 -m unittest discover -s tests -v
python3 bot.py demo
python3 bot.py doctor
python3 bot.py paper
```

`demo` gebruikt verzonnen koersen en zegt niets over rendement. `doctor` doet
alleen read-only verzoeken. `paper` verzamelt echte marktgegevens en Jupiter-
quotes, maar boekt uitsluitend hypothetische aankopen en verkopen. Zonder
betrouwbare gegevens worden geen nieuwe posities geopend.

Open `state/paper-report.html` voor het rapport. Stop met Ctrl+C; hetzelfde
commando hervat dezelfde database. Dit proces moet blijven draaien.

## Strategie en grenzen

Eén regelgestuurde strategie; geen LLM of meerdere AI-modellen in de orderlus.
Solana-tokenontdekking via Jupiter, liquiditeits-/volume-/activiteitsfilters,
minstens vijf minuten koershistorie en 1–6% momentum. Instap pas de volgende
cyclus, na een nieuwe veiligheidscontrole en koop-/verkoopquote.

- Maximaal twee posities van elk maximaal 10 USDC; minstens 20 USDC reserve.
- Stop op 8% verlies per positie; winstdoel 16%; maximale houdtijd één uur.
- Geen nieuwe instappen na 3 USDC dagverlies of 10 USDC totaalverlies.
- Kosten, mogelijke slippage en accountaanmaak worden conservatief gemodelleerd.
- Verkooproute ontbreekt: positie blijft staan, waardering wordt als onzeker
  gemarkeerd, nieuwe instappen worden geblokkeerd.

Stops begrenzen werkelijk verlies niet: een koerssprong of ontbrekende verkooproute
kan veel grotere verliezen veroorzaken. De tokenfilters bieden geen garantie
tegen fraude of een rug pull. Quotes zijn geen bewijs van uitvoerbare transacties.
USDC wordt voor de simulatie gelijkgesteld aan USD.

## Gedeelde Render-worker

`shared_run.py` is bedoeld als apart proces naast een bestaande toepassing:

```sh
python3 shared_run.py
```

De wrapper vereist een echte permanente mount op `/var/data`. Hij schrijft alleen
naar `/var/data/fomo-lab/`, start met lagere CPU-prioriteit en beperkt zijn eigen
virtuele adresruimte tot 192 MiB. Geen mount of te weinig vrije ruimte: exitcode 78.
Een procesbeheerder kan zo de bestaande toepassing laten draaien terwijl deze
paperbot geblokkeerd blijft. Beide processen delen nog wel de server en deployments.

`shared-config.json` rekent uitsluitend de extra schijfkosten van $0,25 per 30 dagen
mee. De reeds betaalde host is een bestaande kostenpost; eventuele API-kosten,
belastingen en extra dataverkeer zijn niet in dat bedrag opgenomen.

De runtime wordt bij de scanner geleverd als een gecontroleerde kopie van deze
repository. Updates worden bewust overgenomen; er wordt tijdens starten geen
code van een bewegende GitHub-branch gedownload.

## Validatie

47 Python-tests slagen, inclusief logging, opslag en dashboardbediening. De tests
gebruiken fixtures. Een historische korte verbindingstest telde acht cycli,
22 snapshots en nul trades; een latere proef kreeg netwerktime-outs. Geen van
beide bewijst rendement. Langdurige marktvalidatie en echte uitvoering ontbreken.

Jupiter documenteert API-authenticatie. Voeg zo nodig `JUPITER_API_KEY` toe aan de
procesomgeving. `.env.example` wordt niet automatisch geladen. Gebruik uitsluitend
een API-key voor marktdata/quotes; deze code accepteert geen walletsleutel.

## Bronnen

- https://developers.jup.ag/docs/swap/order-and-execute
- https://developers.jup.ag/docs/tokens/token-information
- https://solana.com/docs/tokens
- https://render.com/docs/disks
- https://render.com/docs/native-runtimes
- https://render.com/pricing

## Live volgen op Render

Open de bestaande worker in Render en kies **Logs**. Filter op `fomo-paper`.
`SCAN_TOKEN` toont elke door de strategie beoordeelde munt, koers, liquiditeit,
activiteit en de reden om over te slaan of een signaal te maken.
Dit is de geselecteerde kandidatenlijst; het is geen volledige lijst van alle
munten die Jupiter al vóór selectie heeft weggefilterd.
`SIGNAL` is een kandidaat voor de volgende cyclus, nog geen aankoop.
`BUY` en `SELL` zijn fictieve transacties; de extra velden met `_usdc` geven
bedragen in leesbare USDC. `STATUS` toont saldo, posities, resultaat en stoplimieten.
`DATA_ERROR` betekent dat de marktfeed niet goed gelezen kon worden.

De hostlogs tonen alleen vastgelegde databasegebeurtenissen en herhalen bij een
herstart niet het volledige verleden. Render bepaalt de bewaartermijn van logs.
De volledige opgeslagen audit en snapshots blijven in
`/var/data/fomo-lab/paper.sqlite`. Het bijgewerkte HTML-rapport staat in
`/var/data/fomo-lab/paper-report.html`; dit is een bestand op de worker, geen
publieke dashboardwebsite. Het rapport toont de laatste 500 gebeurtenissen.

## Online dashboard (0.2)

Het bestaande Render-webproces biedt `/paper` met een aparte toegangscode,
HttpOnly sessiecookie (12 uur) en controles op verzoekherkomst. Geen API- of
walletsleutels in de browser. De code van de webinterface en de beperkte
Redis-koppeling staat in `Linters57/collector-monitor`.

Het dashboard toont de laatste scan, maximaal 500 transacties, 160 recente
gebeurtenissen en 720 waardemetingen. CSV exporteert de geselecteerde recente
transacties. De complete database blijft op de permanente disk. Redis bevat
slechts één begrensde kopie van maximaal 384 kB en één tijdelijke opdracht;
het is geen permanente tradingdatabase.

Nieuwe aankopen pauzeren/hervatten is mogelijk. Verkoopregels blijven werken.
Instellingen zijn beperkt tot positie-inleg (1–10 USDC), aantal posities (1–2),
stop-loss (5–10%) en take-profit (8–30%). Aanpassen kan alleen zonder open
posities. Verlieslimieten blijven actief en kunnen niet worden gereset via
het dashboard. Een opdracht wordt nooit als toegepast getoond vóór de bot
haar in SQLite heeft vastgelegd. Opdrachten verlopen na 180 seconden;
herstart herstelt de laatst bevestigde pauze en instellingen.

Zonder gekoppelde permanente disk blijft de bot uit. Het online dashboard
is dan bereikbaar, maar toont expliciet dat nog geen data beschikbaar is.

## Startkapitaal op de gedeelde host (0.2.1)

`shared-config.json` stelt het virtuele startkapitaal in op 1.000 USDC.
De expliciet gevraagde verhoging van het bestaande experiment van 100 naar
1.000 wordt onder de proceslock één keer transactioneel toegepast. De audit
krijgt `CAPITAL_CHANGE`; saldo en waarderingsreferenties stijgen met 900,
terwijl bestaande trades, gerealiseerd resultaat en verliesstops behouden
blijven. Dit is geen handelswinst. De online waardegrafiek corrigeert oudere
meetpunten voor deze toevoeging en vermeldt dat zichtbaar.
Andere configuratieverschillen worden niet automatisch geaccepteerd.
