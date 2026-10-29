# MorningBird public showcase

This directory is a static public page for Vercel. It contains only the newspaper image and three edited excerpts from past generated briefings. It does not connect to the local configuration API, Ollama, Telegram, or the user's `.env`.

For Git deployment, import `NSB2023/news-update-teleBot` into Vercel and set **Root Directory** to `showcase`. Use the **Other** framework preset with no build command. Vercel will serve `index.html` and `assets/` as static files.

For CLI deployment after login, run `vercel --cwd showcase` for a preview, then `vercel --prod --cwd showcase` for the public URL.
