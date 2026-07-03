# Prompt dla Grok Build: Aplikacja monitorująca SigenStor

**Zadanie:** Stwórz kompletną, nowoczesną aplikację webową do monitorowania systemu Sigenergy SigenStor.

## Wymagania ogólne
- **Tylko odczyt** – aplikacja ma wyłącznie czytać dane przez Modbus TCP. Żadnych komend zapisu.
- Technologia: **Python + NiceGUI** (lub Streamlit jeśli NiceGUI będzie problematyczny)
- Baza danych: **SQLite** (jeden plik `data/sigenstor.db`)
- Wykresy: **Plotly** (interaktywne, responsywne, ciemny motyw)
- Wygląd: nowoczesny, ciemny motyw (energy/tech style), czysty, profesjonalny, responsywny
- Uruchomienie: prosto – `python main.py`

## Funkcjonalności

### 1. Konfiguracja (zakładka Settings)
- Pole do wpisania:
  - IP SigenStor
  - Port (domyślnie 502)
  - Slave ID (domyślnie 247)
  - Interwał pobierania danych (sekundy, np. 10-60)
- Przycisk "Test Connection" + "Save Config"
- Konfiguracja zapisywana w pliku JSON lub w bazie

### 2. Dashboard Główny (Real-time)
- Aktualne wartości w kartach/gauges:
  - SOC baterii (%)
  - Moc PV (kW)
  - Moc baterii (kW) + kierunek (ładowanie/rozładowanie)
  - Moc z sieci (kW) + kierunek
  - Zużycie domu (kW)
  - Stan systemu (On Grid / Off Grid itp.)
- Sankey diagram lub flow chart pokazujący przepływy energii (PV → Battery/Home/Grid)
- Ostatnie pomiary (timestamp)

### 3. Wykresy Historyczne
- Kilka zakładek lub selektor zakresu czasu (1h, 6h, 24h, 7 dni, 30 dni, custom)
- Wykresy:
  - Moc w czasie (linie: PV, Battery, Grid, Load)
  - SOC baterii w czasie
  - Energia skumulowana (dzienny/tygodniowy bilans)
  - Możliwość zoomu i hover z wartościami

### 4. Podsumowania
- Dziś / Wczoraj / Ten tydzień / Ten miesiąc:
  - Wyprodukowana energia z PV
  - Zużyta z baterii
  - Zużyta z sieci
  - Eksport do sieci
  - Autokonsumpcja %

### 5. Dane Surowe
- Tabela z ostatnimi rekordami z bazy (z możliwością filtrowania i eksportu CSV)

## Techniczne szczegóły

### Modbus
- Użyj biblioteki `pymodbus`
- Czytaj kluczowe rejestry (użyj aktualnej wersji Modbus Protocol Sigenergy – V2.x)
- Przykładowe ważne rejestry (dostosuj na podstawie oficjalnej dokumentacji):
  - SOC baterii
  - Moc PV (całkowita i per string jeśli możliwe)
  - Moc baterii
  - Moc grid / load
  - Statusy, alarmy itp.
- Obsłuż błędy połączenia gracefully (retry, status "Disconnected")

### Background Task
- Asynchroniczne pobieranie danych co X sekund
- Zapisywanie do tabeli z `timestamp`

### Baza Danych (SQLite)
Tabela np. `measurements`:
- `id`
- `timestamp` (DATETIME)
- `soc` (float)
- `pv_power` (float)
- `battery_power` (float)
- `grid_power` (float)
- `load_power` (float)
- `...` (inne ważne parametry)

### UI / UX
- Sidebar z nawigacją (Dashboard, Charts, Summary, Raw Data, Settings)
- Dark mode domyślny
- Ładne ikony (solar, battery, grid)
- Responsywny design (dobrze wygląda na telefonie i komputerze)

## Dodatkowe wymagania
- Pełny kod w jednym repozytorium / folderze gotowy do uruchomienia
- `requirements.txt`
- README.md z instrukcją uruchomienia
- Obsługa błędów i logging
- Możliwość łatwego dodawania nowych rejestru Modbus w przyszłości

Stwórz kompletny projekt zgodnie z powyższym opisem. Zacznij od struktury plików i `main.py`. Użyj najlepszych praktyk Python 2026.
