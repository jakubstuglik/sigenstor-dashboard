# SigenStor Dashboard

Nowoczesna aplikacja webowa do monitorowania systemu magazynowania energii **Sigenergy SigenStor** przez Modbus TCP (tylko odczyt).

## Funkcje

- **Dashboard na żywo**: karty z aktualnymi wartościami (SOC, PV, Bateria, Sieć, Zużycie), diagram Sankey przepływów energii, status systemu.
- **Wykresy historyczne**: interaktywne wykresy Plotly (mocy, SOC, energii) z wyborem zakresu czasu.
- **Podsumowania**: dziś/wczoraj/tydzień/miesiąc – produkcja PV, użycie baterii, import/eksport z sieci, autokonsumpcja.
- **Dane surowe**: tabela ostatnich pomiarów + eksport CSV.
- **Ustawienia**: konfiguracja IP, port, slave ID, interwał odpytywania + test połączenia.
- **SQLite** do przechowywania historii.
- Ciemny, profesjonalny motyw energy/tech.
- Pełna obsługa błędów, logowanie, retry.

## Wymagania

- Python 3.10+
- SigenStor z włączonym Modbus TCP (w aplikacji mySigen: Device → Settings → Modbus TCP Server Enable)
- IP urządzenia dostępne z komputera (domyślnie port 502, slave 247)

## Instalacja i uruchomienie

```bash
# 1. Klonuj lub pobierz repo
cd sigenstor-dashboard

# 2. Utwórz venv i zainstaluj zależności
python -m venv .venv
.venv\Scripts\activate          # Windows (PowerShell: .\.venv\Scripts\Activate.ps1)
# lub source .venv/bin/activate   # Linux/mac

pip install -r requirements.txt

# 3. Uruchom aplikację
python main.py
```

Aplikacja otworzy się w przeglądarce: http://localhost:8080

## Konfiguracja domyślna

- IP: `192.168.33.13`
- Port: `502`
- Slave ID: `247`
- Interwał: `15` sekund

Przejdź do zakładki **Settings**, wpisz poprawne dane i kliknij **Save Config** + **Test Connection**.

## Struktura Modbus (podstawowa)

Aplikacja odczytuje kluczowe rejestry (zgodne z Sigenergy Modbus Protocol ~V1.7/V2.x):

| Parametr       | Adres   | Typ     | Skala   | Jednostka | Uwagi                          |
|----------------|---------|---------|---------|-----------|--------------------------------|
| SOC baterii    | 30014   | uint16  | 0.1     | %         |                                |
| Moc PV         | 30035   | int32   | 0.001   | kW        | Plant Photovoltaic power       |
| Moc baterii    | 30037   | int32   | 0.001   | kW        | + ładowanie, - rozładowanie    |
| Moc sieci      | 30005   | int32   | 0.001   | kW        | + import, - eksport            |
| Status grid    | 30009   | uint16  | 1       | -         | 0=OnGrid, 1/2=OffGrid          |

**Moc zużycia domu (Load)** jest obliczana: `Load = PV + Grid - Battery`

Rejestry można łatwo rozbudować w kodzie (patrz `REGISTERS` w `main.py`).

**Uwaga:** Upewnij się, że używasz poprawnej wersji protokołu Modbus dla Twojego firmware. Niektóre nowsze wersje (V2.8+) dodają bezpośrednie rejestry mocy obciążenia.

## Uruchomienie w tle / produkcja (opcjonalnie)

- Użyj `python main.py --host 0.0.0.0 --port 8080` aby nasłuchiwać na wszystkich interfejsach.
- Dla stałego działania: systemd, docker, lub `nohup python main.py &`

## Rozwój / Dodawanie rejestrów

1. Dodaj wpis w słowniku `REGISTERS` w `main.py`.
2. Zaktualizuj tabelę DB (lub użyj istniejących kolumn + dodatkowe).
3. Dodaj karty/wykresy w UI.
4. Aplikacja jest zaprojektowana tylko do odczytu (read-only).

## Logi i dane

- Baza: `data/sigenstor.db`
- Logi: `logs/` (jeśli skonfigurowane)
- Config: `config.json` (generowany przy zapisie)

## Bezpieczeństwo

- Tylko odczyt – żadne komendy zapisu nie są wysyłane.
- Nie wystawiaj aplikacji publicznie bez autoryzacji (nginx, auth, VPN).

## Licencja

MIT – używaj swobodnie.

---

Stworzone zgodnie z planem w `sigenstor-monitoring-app-prompt.md`. Miłego monitorowania energii! ☀️🔋
