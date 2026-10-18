# NFPL Access Control Service — Production Deployment Guide

This guide provides step-by-step instructions to deploy and host the **NFPL Access Control Service** backend in the cloud.

---

## 🚀 Pre-Configured Cloud Features

The backend is pre-configured for cloud hosting out-of-the-box:
1. **Dynamic Port Binding**: Automatically listens on `$PORT` provided by cloud platforms (Render, Railway, Fly.io, Heroku) with fallback to `8000`.
2. **Global Host Binding**: Binds to `0.0.0.0` so incoming internet traffic reaches the container.
3. **CORS Enabled**: Cross-Origin Resource Sharing (`CORSMiddleware`) is enabled for all origins (`*`) so web applications and external clients can make API calls.
4. **Automated Seeding**: `python run.py seed` builds tables and seeds the 300 deliberate test jobs upon deployment.
5. **Database Flexibility**: Default persistent SQLite, with automatic connection string conversion if PostgreSQL (`DATABASE_URL`) is supplied.

---

## 🌟 Option 1: Deploy on Render (Recommended — Free, Fast, Easiest)

Render provides free hosting directly from your GitHub repository in ~3 minutes.

### Steps:
1. **Push your code to GitHub** (if not already done):
   ```bash
   git add .
   git commit -m "feat: prepare production deployment files"
   git push origin main
   ```
2. Go to **[render.com](https://render.com/)** and sign in with GitHub.
3. On your dashboard, click **"New +"** and select **"Web Service"**.
4. Choose **"Build and deploy from a Git repository"** and select your repository.
5. Configure the service settings:
   * **Name:** `nfpl-access-control` (or your choice)
   * **Region:** Any region close to your users (e.g., *Singapore*, *Frankfurt*, *Oregon*)
   * **Branch:** `main`
   * **Runtime:** `Python 3`
   * **Build Command:**
     ```bash
     pip install -r requirements.txt && python run.py seed
     ```
   * **Start Command:**
     ```bash
     uvicorn src.api.app:app --host 0.0.0.0 --port $PORT
     ```
   * **Instance Type:** `Free`
6. Click **"Create Web Service"**.
7. Wait ~2 minutes for the build to complete. Once deployed, Render gives you a public URL:
   * **Interactive Swagger UI:** `https://<your-service-name>.onrender.com/docs`
   * **Health Check:** `https://<your-service-name>.onrender.com/health`

*(Note: Render will automatically detect `render.yaml` if you choose "Blueprint" deployment).*

---

## 🚂 Option 2: Deploy on Railway

Railway offers automatic zero-config deployments from Git.

### Steps:
1. Go to **[railway.app](https://railway.app/)** and sign in with GitHub.
2. Click **"New Project"** $\to$ **"Deploy from GitHub repo"**.
3. Select your repository.
4. Railway will automatically detect the **`Procfile`** and **`Dockerfile`**.
5. Once deployed, click on your service card $\to$ go to **"Settings"** $\to$ **"Networking"** $\to$ click **"Generate Domain"**.
6. Open your public domain in the browser:
   * `https://<your-domain>.up.railway.app/docs`

---

## 🐳 Option 3: Deploy with Docker / Docker Compose (Any VPS / Linux Server)

Use this method if you have a DigitalOcean Droplet, AWS EC2, Linode, or any private server.

### Steps:
1. SSH into your server:
   ```bash
   ssh user@your-server-ip
   ```
2. Clone your repository:
   ```bash
   git clone https://github.com/<YOUR_USERNAME>/<REPO_NAME>.git
   cd <REPO_NAME>
   ```
3. Run the application in the background using Docker Compose:
   ```bash
   docker compose up -d --build
   ```
4. Verify the container is running and healthy:
   ```bash
   docker compose ps
   curl http://localhost:8000/health
   ```
5. Access your service:
   * Swagger Documentation: `http://<your-server-ip>:8000/docs`

*(Data is persisted inside the named volume `nfpl-data` across container restarts).*

---

## ☁️ Option 4: Deploy on Google Cloud Run

For serverless container deployment that automatically scales:

### Steps:
```bash
# 1. Build and push container to Google Artifact Registry
gcloud builds submit --tag gcr.io/<PROJECT_ID>/nfpl-access-control

# 2. Deploy to Cloud Run
gcloud run deploy nfpl-access-control \
  --image gcr.io/<PROJECT_ID>/nfpl-access-control \
  --platform managed \
  --region asia-south1 \
  --allow-unauthenticated \
  --port 8000
```

---

## 🔍 Post-Deployment Verification Checklist

Once your service is live, run these 4 checks to verify that all systems are operational:

| Check | URL / Action | Expected Result |
| :--- | :--- | :--- |
| **1. Liveness** | `GET https://<your-app>/health` | Status `200 OK`, `{"status": "healthy"}` |
| **2. Root Redirect** | `GET https://<your-app>/` | Redirects to `/docs` |
| **3. Swagger Docs** | `GET https://<your-app>/docs` | Interactive Swagger API interface loads with all endpoints |
| **4. Decision Engine** | `GET https://<your-app>/can?user_id=usr_maker_del&action=job:prepare&job_id=JOB-PLANTED-01` *(Header `X-Actor-Id: usr_admin_1`)* | Status `200 OK`, `{"allowed": true}` |
| **5. Cryptographic Audit** | `GET https://<your-app>/audit/verify` | Status `200 OK`, `{"verified": true}` |

---

## 🔑 Environment Variables Reference (Optional)

| Variable | Default Value | Description |
| :--- | :--- | :--- |
| `PORT` | `8000` | Port listened to by the web server (automatically set by Render/Railway). |
| `HOST` | `0.0.0.0` | IP interface to bind to (allows external connections). |
| `DATABASE_URL` | `sqlite:///data/access_control.db` | Override with PostgreSQL connection string if using cloud SQL. |
