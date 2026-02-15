# Deploying to Cloudflare Pages

## Option 1: Connect GitHub (Recommended)

1. Go to Cloudflare Dashboard > Pages > Create a project
2. Connect your GitHub repo
3. Configure:
   - **Build command:** `npm run build`
   - **Build output directory:** `dist`
   - **Root directory:** `odin-website`
4. Set custom domain: `runsodin.com`

Cloudflare will auto-deploy on every push to master.

## Option 2: Manual Upload

```bash
cd odin-website
npm install
npm run build
```

Upload the `dist/` directory via Cloudflare Pages dashboard.

## SPA Routing

The `public/_redirects` file handles client-side routing — all paths serve `index.html` with a 200 status so React Router works correctly.

## Custom Domain

Point `runsodin.com` to Cloudflare Pages via DNS:
- CNAME record: `runsodin.com` -> `<your-project>.pages.dev`
- Or use Cloudflare's automatic DNS setup when adding the custom domain in Pages settings
