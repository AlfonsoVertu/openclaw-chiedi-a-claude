@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
title SOWAI - Agent PC per Windows

rem ============================================================
rem  Installer dell'agent PC per SOWAI/fitness.
rem
rem  Fa tutto quello che serve, in ordine, e si ferma a spiegare
rem  quando tocca all'utente:
rem    1. controlla Node (serve per Claude Code)
rem    2. installa Claude Code
rem    3. fa fare il LOGIN a Claude (l'utente, col SUO abbonamento)
rem    4. accoppia questo PC al tenant (URL + codice a 6 cifre)
rem    5. avvia l'agent e lo lascia girare
rem
rem  Perche' il login lo fa l'utente e non lo script: l'abbonamento
rem  Claude e' personale. Ogni PC usa il proprio - e' cosi' che ogni
rem  tenant ha il suo Claude senza toccare quello di nessun altro.
rem ============================================================

echo.
echo   ============================================
echo    SOWAI - installazione agent PC
echo   ============================================
echo.

rem --- 1. Node ---
where node >nul 2>&1
if errorlevel 1 (
    echo   [!] Node.js non e' installato, e serve per Claude Code.
    echo       Scaricalo da  https://nodejs.org  ^(versione LTS^), installalo,
    echo       poi rilancia questo file.
    echo.
    pause
    exit /b 1
)
echo   [ok] Node.js trovato.

rem --- 2. Claude Code ---
where claude >nul 2>&1
if errorlevel 1 (
    echo   [..] Installo Claude Code ^(un minuto^)...
    call npm install -g @anthropic-ai/claude-code
    if errorlevel 1 (
        echo   [!] Installazione di Claude Code fallita. Controlla la connessione.
        pause
        exit /b 1
    )
    echo   [ok] Claude Code installato.
) else (
    echo   [ok] Claude Code gia' presente.
)

rem --- 3. login (lo fa l'utente) ---
echo.
echo   ------------------------------------------------
echo    ORA DEVI ACCEDERE A CLAUDE COL TUO ABBONAMENTO.
echo    Si aprira' il browser: accedi e torna qui.
echo   ------------------------------------------------
echo.
pause
call claude auth
echo.

rem verifica che il login sia andato
echo   [..] Verifico l'accesso...
echo rispondi solo: ok | claude -p "rispondi solo: ok" >nul 2>&1
if errorlevel 1 (
    echo   [!] L'accesso a Claude non risulta completo.
    echo       Riprova il comando:  claude auth
    echo       poi rilancia questo file.
    pause
    exit /b 1
)
echo   [ok] Claude accede correttamente.

rem --- 4. accoppiamento al tenant ---
echo.
echo   ------------------------------------------------
echo    Collega questo PC al tuo gestionale.
echo   ------------------------------------------------
set /p SERVER=   Indirizzo del tuo SOWAI (es. https://fitness.workingwithweb.eu):
set /p CODICE=   Codice di accoppiamento (6 cifre, dalla backend):

python "%~dp0agent.py" accoppia "%SERVER%" "%CODICE%"
if errorlevel 1 (
    echo   [!] Accoppiamento non riuscito. Controlla indirizzo e codice ^(il codice dura 15 minuti^).
    pause
    exit /b 1
)

rem --- 5. servizio all'avvio + avvio ora ---
echo.
echo   [..] Configuro l'avvio automatico...
rem un'attivita' pianificata che riparte a ogni accesso di Windows
schtasks /create /tn "SOWAI Agent PC" /tr "python \"%~dp0agent.py\"" /sc onlogon /f >nul 2>&1
if errorlevel 1 (
    echo   [i] Non ho potuto creare l'avvio automatico ^(servono permessi admin^).
    echo       L'agent parte comunque adesso; per l'avvio automatico rilancia da amministratore.
) else (
    echo   [ok] Avvio automatico configurato: l'agent ripartira' a ogni accesso.
)

echo.
echo   ============================================
echo    Fatto. L'agent e' attivo. Puoi chiudere,
echo    ma se chiudi questa finestra l'agent si ferma
echo    fino al prossimo accesso a Windows.
echo   ============================================
echo.
python "%~dp0agent.py"
