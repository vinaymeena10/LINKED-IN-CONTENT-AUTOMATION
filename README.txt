LINKEDIN GROWTH STUDIO — PREMIUM UPDATE

Included:
- Premium dark navy / violet / cyan dashboard and job-search redesign.
- Expanded starter directory of 50 public ATS company-board sources.
- Country selector (India, US, UK, Canada, Australia, Germany, France, Netherlands, Singapore, UAE, Remote/Worldwide, Any country).
- Automatic ATS detection for Greenhouse, Lever and Ashby from a company careers URL.
- Per-post image generation using the post topic/category and content, via the existing OpenAI Images API route.
- Human approval remains required before publishing.

DEPLOY:
1. Back up your current app.py and templates folder.
2. Replace app.py with this app.py.
3. Replace templates/index.html and templates/jobs.html with these versions.
4. Keep your existing L_requirements.txt and Dockerfile.
5. In Render, keep APP_ENCRYPTION_KEY unchanged.
6. Set OPENAI_API_KEY in Render to enable generated post images. Optional: OPENAI_IMAGE_MODEL (default gpt-image-1).
7. Deploy and test /, /jobs, ATS detection, image generation, and LinkedIn OAuth.

IMPORTANT:
- ATS detection currently supports Greenhouse, Lever and Ashby only; it is not a crawler of every company on the internet. Workday and custom ATS pages need separate integrations.
- Public job-board identifiers can change. Some starter directory entries may need to be verified or removed if a provider returns 404.
- Image files are stored locally under static/generated. Configure a persistent Render disk mounted to the app's working directory if generated images must survive deploys/restarts.
- Do not change APP_ENCRYPTION_KEY if you want existing encrypted LinkedIn tokens to remain readable.
- Country selection filters based on the location text returned by each public board; listings with incomplete location metadata may be missed.
