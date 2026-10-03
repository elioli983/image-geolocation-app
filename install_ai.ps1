$ErrorActionPreference = "Stop"
Write-Host "Installing AI dependencies for Image Geolocation Estimation App v2.4..."
python -m pip install --upgrade pip
python -m pip install -r .\requirements-ai.txt
Write-Host ""
Write-Host "AI dependencies installed. The first DINOv2 ranking run will download facebook/dinov2-small from Hugging Face."
