# Matrix G2 — app occhiali (Even Hub WebView)

App WebView (TypeScript + Vite) che gira sugli occhiali **Even Realities G2** via
Even Hub SDK. Fa solo **I/O del device**: disegna l'HUD, cattura i gesti (tap /
double-tap / swipe) e lo stream PCM del microfono, e li inoltra al **bridge** via
WebSocket. Tutta la logica Matrix (login, E2EE, sync, invio, STT) sta nel bridge
(`../matrix_bridge.py`).

```
occhiali G2 ──BLE──▶ telefono (app Even, hub) ──WebSocket/VPN──▶ bridge (PC)
```

## Sviluppo in un browser (senza occhiali)

Il simulatore ufficiale è un'app desktop nativa (serve GPU/display). Per iterare
in fretta c'è un **mock dell'SDK** (`src/dev-sdk-mock.ts`) che fa girare l'app in
un browser qualsiasi:

```bash
npm install
VITE_MOCK=1 npm run dev -- --host      # apri http://localhost:5173
```

Il mock valida layout e flusso, **non** le quirk BLE/ottiche reali.

## Config

Copia `.env.example` in `.env` e imposta `VITE_BRIDGE_URL` con l'URL del tuo bridge.

## Build & pack per il device

Prima di pubblicare, in `app.json` metti un tuo `package_id` (es.
`com.tuodominio.matrixg2` — il default `com.example.matrixg2` è un placeholder).

```bash
npm run typecheck        # tsc --noEmit
npm run build            # vite build → dist/
npx evenhub pack app.json dist -o matrix-g2.ehpk
```

Poi carica `matrix-g2.ehpk` come build **privata** sul portale Even Hub, oppure
fai sideload via QR:

```bash
npx evenhub qr --url http://<tuo-ip>:5173
```

## Controlli HUD

| Gesto | ROOMS (lista chat) | CHAT (dentro una chat) |
|-------|--------------------|------------------------|
| swipe su/giù | muovi il cursore | scorri i messaggi |
| tap | apri la chat selezionata | avvia la dettatura (mic) |
| double-tap | spegni l'HUD | torna alla lista |

**Dettatura**: **tap** per iniziare a registrare, **tap** di nuovo per fermare →
l'HUD mostra "trascrivo…", poi appare il testo trascritto. Da lì: **swipe** per
scorrere il testo, **1 tap** per inviare, **2 tap** per scartare.

## File

- `src/main.ts` — app: WebSocket col bridge, HUD (`rebuildPageContainer`), gesti, mic.
- `src/dev-sdk-mock.ts` — mock SDK per il browser (solo `VITE_MOCK=1`).
- `app.json` — manifest Even Hub (permessi `network` + `g2-microphone`, versione).
