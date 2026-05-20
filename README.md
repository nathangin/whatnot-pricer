# Whatnot Pricer

Real-time Pokemon card price overlay for Whatnot live streams. Captures a region of your screen, identifies cards via Claude vision, and shows TCGPlayer market prices in a floating window.

![overlay demo](https://i.imgur.com/placeholder.png)

## Features

- Drag to select any region of your screen to monitor
- Claude vision detects card name, set, holo status, grade, and stream price
- Looks up TCGPlayer market/low/high prices via pokemontcg.io (free, no key needed)
- Deal indicator: 🔥 Great Deal / ✅ Good / ⚖️ Fair / ⚠️ Overpriced
- Always-on-top floating overlay, draggable, semi-transparent
- Scrollable session log of last 20 cards
- Skips unchanged frames and caches repeated lookups — stays fast during rapid sales
- Foreign card detection (🇯🇵🇨🇳🇰🇷) with a note that TCGPlayer prices are English only

## Requirements

- Python 3.10+
- Anaconda or standard Python install
- Anthropic API key (get one free at [console.anthropic.com](https://console.anthropic.com))

## Setup

```powershell
pip install -r requirements.txt
```

Create a `.env` file in the project folder:

```
ANTHROPIC_API_KEY=sk-ant-your-key-here
```

## Run

```powershell
python main.py
```

On first launch, drag to select the Whatnot stream region. The selection is saved and reused on next launch. Press **↺** in the overlay to re-select.

## Cost

Each card scan costs ~$0.004 in Claude API credits. A typical 3-hour Whatnot session (~150 cards) runs about $0.60 total.

## Files

| File | Purpose |
|------|---------|
| `main.py` | Entry point, region selector |
| `monitor.py` | Screen capture loop + Claude vision |
| `prices.py` | pokemontcg.io lookup with 10-min cache |
| `overlay.py` | Tkinter floating overlay window |
