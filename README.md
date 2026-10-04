# BSR Corridors — hotel website

Static one-page site built with Next.js (static export → `out/`).

## Update the content
Edit **`content/hotel.ts`** — name, tagline, contact numbers, address, map, rooms,
amenities, policies and photos all live there. Anything still in `[brackets]`
shows on the site with a dashed "placeholder" marker.

Photos: copy them into `public/images/` (e.g. `public/images/lobby.jpg`) and set
the matching `src` in `content/hotel.ts` to `/images/lobby.jpg`. Write a short
`alt` description for each. Keep each photo under ~500 KB (resize to ~1920px wide
for the hero, ~1200px for others) so the site loads fast on mobile.

WhatsApp and call buttons appear automatically once `whatsappNumber` and `phone`
are filled in.

## Run locally
```bash
npm install
npm run dev        # http://localhost:3000
npm run build      # writes the static site to out/
npm start          # serves out/ (what Heroku runs)
```

## Deploy
**Netlify (recommended for a static site):** connect the repo — `netlify.toml`
already sets build command `npm run build` and publish directory `out`.
Or drag-and-drop the `out/` folder at app.netlify.com/drop.

**Heroku:** push the repo to a Heroku app with the Node.js buildpack. Heroku runs
`npm run build`, then the `Procfile` starts `serve` on Heroku's `$PORT`.
