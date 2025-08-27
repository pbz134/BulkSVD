#!/usr/bin/env python3
"""
Google Street View Panorama Tile Downloader with GUI
Downloads only valid tiles based on known patterns for each zoom level.
Deletes tiles after stitching and names output with coordinates and ID.
"""

import argparse
import os
import re
import requests
import sys
import csv
import json
import base64
import time
import math
from urllib.parse import urlparse, parse_qs
import concurrent.futures
from PIL import Image
import shutil

# GUI imports
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QLabel, QLineEdit, QComboBox, QPushButton, 
                             QProgressBar, QTextEdit, QFileDialog, QGroupBox, 
                             QMessageBox, QCheckBox, QSpinBox, QTabWidget, QStyleFactory,
                             QListWidget, QListWidgetItem, QTableWidget, QTableWidgetItem,
                             QHeaderView, QSplitter, QDoubleSpinBox, QGridLayout)
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QFont, QPalette, QColor, QIcon

# Selenium imports for area scanning
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException
from selenium.webdriver.common.action_chains import ActionChains
import threading


def auto_crop_panorama(image, black_threshold=10):
    """
    Automatically crop black borders from the panorama
    """
    # Convert to grayscale for edge detection
    gray = image.convert('L')
    pixels = gray.load()
    
    width, height = image.size
    
    # Find top non-black row
    top = 0
    for y in range(height):
        row_avg = sum(pixels[x, y] for x in range(width)) / width
        if row_avg > black_threshold:
            top = y
            break
    
    # Find bottom non-black row
    bottom = height - 1
    for y in range(height - 1, -1, -1):
        row_avg = sum(pixels[x, y] for x in range(width)) / width
        if row_avg > black_threshold:
            bottom = y
            break
    
    # Find left non-black column
    left = 0
    for x in range(width):
        col_avg = sum(pixels[x, y] for y in range(height)) / height
        if col_avg > black_threshold:
            left = x
            break
    
    # Find right non-black column
    right = width - 1
    for x in range(width - 1, -1, -1):
        col_avg = sum(pixels[x, y] for y in range(height)) / height
        if col_avg > black_threshold:
            right = x
            break
    
    # Add some padding to ensure we don't crop too aggressively
    padding = 10
    left = max(0, left - padding)
    right = min(width - 1, right + padding)
    top = max(0, top - padding)
    bottom = min(height - 1, bottom + padding)
    
    # Only crop if we found valid bounds
    if (right > left and bottom > top and 
        (left > 0 or right < width - 1 or top > 0 or bottom < height - 1)):
        print(f"Cropping panorama from {width}x{height} to {right-left+1}x{bottom-top+1}")
        return image.crop((left, top, right + 1, bottom + 1))
    
    return image

def crop_tile_borders(tile_path):
    """
    Crop black borders from a single tile
    Returns the cropped image or None if the tile is completely black
    """
    try:
        with Image.open(tile_path) as img:
            # Convert to grayscale for edge detection
            gray = img.convert('L')
            pixels = gray.load()
            
            width, height = img.size
            
            # Find non-black content boundaries
            left, right, top, bottom = 0, width-1, 0, height-1
            
            # Find left boundary
            for x in range(width):
                col_avg = sum(pixels[x, y] for y in range(height)) / height
                if col_avg > 10:  # Threshold for non-black
                    left = x
                    break
            
            # Find right boundary
            for x in range(width-1, -1, -1):
                col_avg = sum(pixels[x, y] for y in range(height)) / height
                if col_avg > 10:
                    right = x
                    break
            
            # Find top boundary
            for y in range(height):
                row_avg = sum(pixels[x, y] for x in range(width)) / width
                if row_avg > 10:
                    top = y
                    break
            
            # Find bottom boundary
            for y in range(height-1, -1, -1):
                row_avg = sum(pixels[x, y] for x in range(width)) / width
                if row_avg > 10:
                    bottom = y
                    break
            
            # Check if tile has any content
            if right <= left or bottom <= top:
                return None
                
            # Crop the tile
            cropped = img.crop((left, top, right + 1, bottom + 1))
            return cropped
            
    except Exception as e:
        print(f"Error cropping tile {tile_path}: {e}")
        return None

def get_actual_tile_dimensions(zoom):
    """
    Return the actual content dimensions for each zoom level
    based on known patterns of black borders
    """
    # These are the typical content dimensions after removing black borders
    content_dimensions = {
        0: (416, 320),   # Original 512x512, content ~416x320
        1: (416, 320),   # Same as zoom 0
        2: (416, 320),   # Same as zoom 0  
        3: (416, 320),   # Same as zoom 0
        4: (416, 320),   # Same as zoom 0
        5: (416, 320)    # Same as zoom 0
    }
    
    if zoom in content_dimensions:
        return content_dimensions[zoom]
    else:
        return (416, 320)  # Default

def stitch_panorama(panorama_id, zoom, tiles_dir, input_url=None, delete_after_stitch=True):
    """
    Stitch all downloaded tiles into a complete panorama image
    Automatically crops black borders from each tile before stitching
    """
    output_filename = generate_output_filename(panorama_id, zoom, tiles_dir, input_url)
    
    x_min, x_max, y_min, y_max, _ = get_valid_tile_range(zoom)
    
    # Get actual content dimensions after cropping
    content_width, content_height = get_actual_tile_dimensions(zoom)
    
    width_tiles = x_max - x_min + 1
    height_tiles = y_max - y_min + 1
    
    # Calculate final panorama size based on content dimensions
    panorama_width = width_tiles * content_width
    panorama_height = height_tiles * content_height
    
    print(f"Stitching {width_tiles}x{height_tiles} tiles into {panorama_width}x{panorama_height} panorama...")
    print(f"Each tile cropped to {content_width}x{content_height} (removed black borders)")
    
    panorama = Image.new('RGB', (panorama_width, panorama_height))
    missing_tiles = 0
    black_tiles = 0
    
    for y in range(y_min, y_max + 1):
        for x in range(x_min, x_max + 1):
            tile_path = os.path.join(tiles_dir, f"tile_z{zoom}_x{x}_y{y}.jpg")
            
            if os.path.exists(tile_path):
                try:
                    # Crop black borders from the tile
                    cropped_tile = crop_tile_borders(tile_path)
                    
                    if cropped_tile is None:
                        black_tiles += 1
                        print(f"Skipping black tile: x={x}, y={y}")
                        continue
                    
                    # Resize to standard content dimensions if needed
                    if cropped_tile.size != (content_width, content_height):
                        cropped_tile = cropped_tile.resize((content_width, content_height), Image.LANCZOS)
                    
                    # Calculate position in final panorama
                    left = (x - x_min) * content_width
                    top = (y - y_min) * content_height
                    
                    panorama.paste(cropped_tile, (left, top))
                    
                except Exception as e:
                    print(f"Error processing tile {tile_path}: {e}")
                    missing_tiles += 1
            else:
                missing_tiles += 1
    
    if missing_tiles > 0:
        print(f"Warning: {missing_tiles} tiles were missing")
    if black_tiles > 0:
        print(f"Warning: {black_tiles} black/invalid tiles were skipped")
    
    # Final auto-crop to remove any remaining borders
    panorama = auto_crop_panorama(panorama)
    
    panorama.save(output_filename, 'JPEG', quality=95)
    print(f"Panorama saved as: {output_filename}")
    
    if delete_after_stitch:
        print("Deleting tile files...")
        delete_tiles(tiles_dir, zoom)
    
    return output_filename

def download_tile(panorama_id, zoom, x, y, output_dir):
    """
    Download a single tile and save it to the output directory
    Returns True if successful, False if failed
    """
    # Check if panorama_id is a coordinates-based ID
    if panorama_id.startswith("coordinates_"):
        # Extract coordinates from the ID
        try:
            coords_part = panorama_id.replace("coordinates_", "")
            lat, lng = coords_part.split("_")
            # Use a different URL format for coordinates-based panoramas
            url = f"https://cbk0.google.com/cbk?output=tile&cb_client=apiv3&authuser=0&hl=en&gl=us&zoom={zoom}&x={x}&y={y}&n=0&lyrs=pano&location={lat},{lng}"
        except:
            # Fallback to regular URL
            url = f"https://cbk0.google.com/cbk?output=tile&panoid={panorama_id}&zoom={zoom}&x={x}&y={y}"
    else:
        # Regular panorama ID
        url = f"https://cbk0.google.com/cbk?output=tile&panoid={panorama_id}&zoom={zoom}&x={x}&y={y}"
    
    filename = os.path.join(output_dir, f"tile_z{zoom}_x{x}_y{y}.jpg")
    
    try:
        response = requests.get(url, timeout=30)
        
        if response.status_code == 204:
            return False
        
        response.raise_for_status()
        
        with open(filename, 'wb') as f:
            f.write(response.content)
        
        # Don't check for black tiles here - we'll handle them during stitching
        # This allows us to download all tiles and then properly crop them
        
        print(f"Downloaded: {filename}")
        return True
        
    except requests.RequestException as e:
        if "204" not in str(e):
            print(f"Failed to download tile x={x}, y={y}: {e}")
        # Clean up any partially downloaded files
        if os.path.exists(filename):
            os.remove(filename)
        return False

def get_full_street_view_url(viewpoint_url, wait_time=1.0):
    """
    Convert a simple viewpoint URL to a full Street View URL
    by simulating viewport interaction
    
    Args:
        viewpoint_url: URL pattern: https://www.google.com/maps/@?api=1&map_action=pano&viewpoint=lat,lng
        wait_time: Time to wait before returning URL (default: 1 second)
        
    Returns:
        Full Street View URL or None if conversion fails
    """
    # Set up Selenium options
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    
    try:
        # Initialize the driver
        from selenium.webdriver.chrome.service import Service
        service = Service(executable_path="chromedriver")  # Default path, adjust if needed
        driver = webdriver.Chrome(service=service, options=chrome_options)
        
        # Navigate to the viewpoint URL
        driver.get(viewpoint_url)
        
        # Wait for the page to load
        time.sleep(2)
        
        # Handle cookie consent if it appears
        cookie_buttons = driver.find_elements(By.XPATH, 
            "//button[contains(@aria-label, 'Alle akzeptieren') or " +
            "contains(@aria-label, 'Accept all') or " +
            "contains(text(), 'Alle akzeptieren') or " +
            "contains(text(), 'Accept all')]"
        )
        
        if cookie_buttons:
            driver.execute_script("arguments[0].click();", cookie_buttons[0])
            time.sleep(1)
        
        # Simulate looking around to trigger full URL loading
        # Find the Street View container
        time.sleep(1)
            
        # Wait for the URL to update to the full format
        start_time = time.time()
        while time.time() - start_time < wait_time:
            current_url = driver.current_url
            # Check if URL has been updated with full Street View information
            if ("!1s" in current_url or "panoid=" in current_url or 
                "data=" in current_url or "viewpoint=" not in current_url):
                driver.quit()
                return current_url
            time.sleep(0.1)
        
        # Return the current URL even if it might not be fully loaded
        full_url = driver.current_url
        driver.quit()
        return full_url if full_url != viewpoint_url else None
        
    except WebDriverException as e:
        print(f"WebDriver error: {e}")
        return None
    except Exception as e:
        print(f"Error converting URL: {e}")
        return None

def extract_panorama_id_and_coords(input_string):
    """
    Extract panorama ID and coordinates from either a direct ID, Google Maps URL, or viewpoint URL
    Returns (panorama_id, latitude, longitude)
    """
    # If it looks like a direct ID (alphanumeric with possible dashes)
    if re.match(r'^[a-zA-Z0-9_-]+$', input_string):
        return input_string, None, None
    
    # Check if it's a viewpoint URL that needs conversion
    viewpoint_match = re.search(r'viewpoint=([-+]?\d+\.\d+),([-+]?\d+\.\d+)', input_string)
    latitude, longitude = None, None
    if viewpoint_match:
        try:
            latitude = float(viewpoint_match.group(1))
            longitude = float(viewpoint_match.group(2))
            print(f"Found viewpoint URL with coordinates: {latitude:.8f}, {longitude:.8f}")
            
            # Convert to full URL
            full_url = get_full_street_view_url(input_string)
            if full_url and full_url != input_string:
                print("Converted URL to full Street View format")
                # Recursively process the full URL
                return extract_panorama_id_and_coords(full_url)
            else:
                # If conversion failed, return None for panorama ID
                return None, latitude, longitude
        except ValueError:
            pass
    
    # If we didn't process it as a viewpoint URL, continue with original logic
    # Try to extract coordinates from URL (pattern: @lat,lng)
    coords_match = re.search(r'@([-+]?\d+\.\d+),([-+]?\d+\.\d+)', input_string)
    if coords_match and latitude is None:
        try:
            latitude = float(coords_match.group(1))
            longitude = float(coords_match.group(2))
        except ValueError:
            pass
    
    # Try to extract panorama ID from URL
    url_patterns = [
        r'panoid=([a-zA-Z0-9_-]+)',  # panoid parameter
        r'1s([a-zA-Z0-9_-]+)!2e0',   # Common pattern in Google Maps URLs
        r'!1s([a-zA-Z0-9_-]+)',      # Another common pattern
        r'pano=([a-zA-Z0-9_-]+)'     # pano parameter
    ]
    
    panorama_id = None
    for pattern in url_patterns:
        match = re.search(pattern, input_string)
        if match:
            panorama_id = match.group(1)
            break
    
    # If no ID found in patterns, try parsing as URL
    if panorama_id is None:
        try:
            parsed_url = urlparse(input_string)
            # Check if the ID might be in the path
            path_parts = parsed_url.path.split('/')
            for part in path_parts:
                if re.match(r'^[a-zA-Z0-9_-]{20,}$', part):  # IDs are usually long
                    panorama_id = part
                    break
            
            # Check query parameters
            if panorama_id is None:
                query_params = parse_qs(parsed_url.query)
                for key, values in query_params.items():
                    for value in values:
                        if re.match(r'^[a-zA-Z0-9_-]{20,}$', value):
                            panorama_id = value
                            break
                    if panorama_id:
                        break
        except:
            pass
    
    # If we have coordinates but no ID, we'll handle this in the download function
    if panorama_id is None and latitude is not None and longitude is not None:
        return None, latitude, longitude
    
    if panorama_id is None:
        raise ValueError(f"Could not extract panorama ID from: {input_string}")
    
    return panorama_id, latitude, longitude

def meters_to_degrees(meters, latitude):
    """
    Convert meters to approximate degrees for latitude and longitude
    latitude: used to adjust longitude conversion (more accurate near poles)
    """
    # 1 degree latitude ≈ 111,320 meters
    lat_degrees = meters / 111320.0
    
    # 1 degree longitude ≈ 111,320 * cos(latitude) meters
    lon_degrees = meters / (111320.0 * abs(math.cos(math.radians(latitude))))
    
    return lat_degrees, lon_degrees

def get_panorama_coordinates(panorama_id, input_url=None):
    """
    Try to get coordinates for a panorama ID using various methods
    """
    # First try to extract from input URL if provided
    if input_url:
        panorama_id_from_url, lat, lng = extract_panorama_id_and_coords(input_url)
        if lat is not None and lng is not None:
            return lat, lng
    
    # Fallback: try Google Street View API (may not work without API key)
    try:
        metadata_url = f"https://maps.googleapis.com/maps/api/streetview/metadata?pano={panorama_id}"
        response = requests.get(metadata_url, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if data.get('status') == 'OK' and 'location' in data:
                lat = data['location']['lat']
                lng = data['location']['lng']
                return lat, lng
    except:
        pass
    
    return None, None

def get_valid_tile_range(zoom):
    """
    Return the valid tile range for a given zoom level based on known patterns.
    For most Google Street View panoramas, the valid tiles are in these ranges:
    """
    valid_ranges = {
        0: (0, 12, 0, 6),   # x_min, x_max, y_min, y_max (13×7 tiles)
        1: (0, 25, 0, 13),  # 26×14 tiles
        2: (0, 51, 0, 27),  # 52×28 tiles  
        3: (0, 103, 0, 55), # 104×56 tiles
        4: (0, 15, 0, 7),   # 16×8 tiles
        5: (0, 31, 0, 15)   # 32×16 tiles
    }
    
    if zoom in valid_ranges:
        x_min, x_max, y_min, y_max = valid_ranges[zoom]
        x_count = x_max - x_min + 1
        y_count = y_max - y_min + 1
        return x_min, x_max, y_min, y_max, x_count * y_count
    else:
        x_min, x_max, y_min, y_max = 0, 15, 0, 7
        x_count = x_max - x_min + 1
        y_count = y_max - y_min + 1
        return x_min, x_max, y_min, y_max, x_count * y_count

def download_tile(panorama_id, zoom, x, y, output_dir):
    """
    Download a single tile and save it to the output directory
    """
    # Check if panorama_id is a coordinates-based ID
    if panorama_id.startswith("coordinates_"):
        # Extract coordinates from the ID
        try:
            coords_part = panorama_id.replace("coordinates_", "")
            lat, lng = coords_part.split("_")
            # Use a different URL format for coordinates-based panoramas
            url = f"https://cbk0.google.com/cbk?output=tile&cb_client=apiv3&authuser=0&hl=en&gl=us&zoom={zoom}&x={x}&y={y}&n=0&lyrs=pano&location={lat},{lng}"
        except:
            # Fallback to regular URL
            url = f"https://cbk0.google.com/cbk?output=tile&panoid={panorama_id}&zoom={zoom}&x={x}&y={y}"
    else:
        # Regular panorama ID
        url = f"https://cbk0.google.com/cbk?output=tile&panoid={panorama_id}&zoom={zoom}&x={x}&y={y}"
    
    filename = os.path.join(output_dir, f"tile_z{zoom}_x{x}_y{y}.jpg")
    
    try:
        response = requests.get(url, timeout=30)
        
        if response.status_code == 204:
            return False
        
        response.raise_for_status()
        
        with open(filename, 'wb') as f:
            f.write(response.content)
        
        print(f"Downloaded: {filename}")
        return True
        
    except requests.RequestException as e:
        if "204" not in str(e):
            print(f"Failed to download tile x={x}, y={y}: {e}")
        return False

def delete_tiles(tiles_dir, zoom):
    """
    Delete all tile files for the given zoom level
    """
    deleted_count = 0
    for filename in os.listdir(tiles_dir):
        if filename.startswith(f"tile_z{zoom}_") and filename.endswith('.jpg'):
            try:
                os.remove(os.path.join(tiles_dir, filename))
                deleted_count += 1
            except Exception as e:
                print(f"Error deleting file {filename}: {e}")
    
    if deleted_count > 0:
        print(f"Deleted {deleted_count} tile files")
    return deleted_count

def format_coordinate(coord):
    """
    Format coordinate to 8 decimal places
    """
    if coord is None:
        return "unknown"
    return f"{coord:.8f}"

def generate_output_filename(panorama_id, zoom, tiles_dir, input_url=None):
    """
    Generate output filename in format: "lat, lng, panorama_id.jpg"
    """
    # Try to get coordinates from various sources
    lat, lng = get_panorama_coordinates(panorama_id, input_url)
    
    if lat is not None and lng is not None:
        filename = f"{format_coordinate(lat)}, {format_coordinate(lng)}, {panorama_id}.jpg"
    else:
        filename = f"{panorama_id}_z{zoom}.jpg"
    
    return os.path.join(tiles_dir, filename)

def download_panorama_tiles(panorama_input, zoom, output_dir, max_workers=4):
    """
    Download only the valid tiles for the panorama at specified zoom level
    """
    # First extract any available information
    panorama_id, lat, lng = extract_panorama_id_and_coords(panorama_input)
    
    # If we have coordinates but no ID, try to get the panorama ID
    if panorama_id is None and lat is not None and lng is not None:
        print(f"Extracted coordinates: {lat:.8f}, {lng:.8f}")
        print("Getting panorama ID from coordinates...")
        
        # Try to get panorama ID using the metadata API
        try:
            metadata_url = f"https://maps.googleapis.com/maps/api/streetview/metadata?location={lat},{lng}"
            response = requests.get(metadata_url, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                if data.get('status') == 'OK' and 'pano_id' in data:
                    panorama_id = data['pano_id']
                    print(f"Found panorama ID: {panorama_id}")
                else:
                    # Try to use the coordinates as a fallback
                    panorama_id = f"coordinates_{lat}_{lng}"
                    print(f"No panorama ID found, using coordinates-based ID: {panorama_id}")
            else:
                # Fallback to coordinates-based ID
                panorama_id = f"coordinates_{lat}_{lng}"
                print(f"Metadata API failed, using coordinates-based ID: {panorama_id}")
                
        except Exception as e:
            print(f"Error getting panorama ID from coordinates: {str(e)}")
            # Fallback: try to construct a URL that might work
            panorama_id = f"coordinates_{lat}_{lng}"
    
    os.makedirs(output_dir, exist_ok=True)
    
    x_min, x_max, y_min, y_max, total_tiles = get_valid_tile_range(zoom)
    
    print(f"Downloading {total_tiles} valid tiles for panorama {panorama_id} at zoom level {zoom}")
    print(f"Tile range: x={x_min}-{x_max}, y={y_min}-{y_max}")
    print(f"Final panorama size: {(x_max - x_min + 1) * 512} × {(y_max - y_min + 1) * 512} pixels")
    print(f"Tiles will be saved to: {output_dir}")
    
    downloaded_count = 0
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        for y in range(y_min, y_max + 1):
            for x in range(x_min, x_max + 1):
                futures.append(
                    executor.submit(download_tile, panorama_id, zoom, x, y, output_dir)
                )
        
        for future in concurrent.futures.as_completed(futures):
            if future.result():
                downloaded_count += 1
    
    print(f"\nDownload completed! Downloaded {downloaded_count} of {total_tiles} tiles")
    return downloaded_count

# GUI Classes
class DownloadThread(QThread):
    """Thread for downloading and processing panorama"""
    progress_signal = pyqtSignal(int, int, str)
    finished_signal = pyqtSignal(bool, str)
    log_signal = pyqtSignal(str)
    
    def __init__(self, panorama_input, zoom, output_dir, workers, keep_tiles):
        super().__init__()
        self.panorama_input = panorama_input
        self.zoom = zoom
        self.output_dir = output_dir
        self.workers = workers
        self.keep_tiles = keep_tiles
        self.panorama_id = None
        
def run(self):
    try:
        # Extract panorama ID or coordinates
        self.panorama_id, lat, lng = extract_panorama_id_and_coords(self.panorama_input)
        
        # If we have coordinates but no ID, try to get the panorama ID
        if self.panorama_id is None and lat is not None and lng is not None:
            self.log_signal.emit(f"Extracted coordinates: {lat:.8f}, {lng:.8f}")
            self.log_signal.emit("Getting panorama ID from coordinates...")
            
            # Try to get panorama ID using the metadata API
            try:
                metadata_url = f"https://maps.googleapis.com/maps/api/streetview/metadata?location={lat},{lng}"
                response = requests.get(metadata_url, timeout=10)
                
                if response.status_code == 200:
                    data = response.json()
                    if data.get('status') == 'OK' and 'pano_id' in data:
                        self.panorama_id = data['pano_id']
                        self.log_signal.emit(f"Found panorama ID: {self.panorama_id}")
                    else:
                        raise ValueError(f"No panorama found at coordinates: {lat}, {lng}")
                else:
                    raise ValueError("Failed to access metadata API")
                    
            except Exception as e:
                self.log_signal.emit(f"Error getting panorama ID from coordinates: {str(e)}")
                # Fallback: try to construct a URL that might work
                self.panorama_id = f"coordinates_{lat}_{lng}"
        else:
            self.log_signal.emit(f"Extracted panorama ID: {self.panorama_id}")
            if lat is not None and lng is not None:
                self.log_signal.emit(f"Extracted coordinates: {lat:.8f}, {lng:.8f}")
        
        # Get tile range for progress calculation
        x_min, x_max, y_min, y_max, total_tiles = get_valid_tile_range(self.zoom)
        self.log_signal.emit(f"Downloading {total_tiles} tiles for zoom level {self.zoom}")
        
        # Create output directory
        os.makedirs(self.output_dir, exist_ok=True)
        
        # Download tiles
        downloaded_count = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = []
            for y in range(y_min, y_max + 1):
                for x in range(x_min, x_max + 1):
                    futures.append(
                        executor.submit(self.download_tile_with_progress, x, y)
                    )
            
            for i, future in enumerate(concurrent.futures.as_completed(futures)):
                if future.result():
                    downloaded_count += 1
                progress = int((i + 1) / len(futures) * 100)
                self.progress_signal.emit(progress, downloaded_count, f"Downloading tile {i+1}/{len(futures)}")
        
        if downloaded_count > 0:
            self.progress_signal.emit(100, downloaded_count, "Stitching panorama...")
            self.log_signal.emit("Stitching tiles...")
            
            # Stitch panorama
            stitched_file = stitch_panorama(
                self.panorama_id, self.zoom, self.output_dir, 
                self.panorama_input, not self.keep_tiles
            )
            
            if stitched_file:
                self.log_signal.emit(f"Successfully created panorama: {stitched_file}")
                self.finished_signal.emit(True, f"Panorama saved as: {os.path.basename(stitched_file)}")
            else:
                self.log_signal.emit("Failed to stitch panorama")
                self.finished_signal.emit(False, "Failed to stitch panorama")
        else:
            self.log_signal.emit("No tiles were downloaded!")
            self.finished_signal.emit(False, "No tiles were downloaded!")
            
    except Exception as e:
        self.log_signal.emit(f"Error: {str(e)}")
        self.finished_signal.emit(False, f"Error: {str(e)}")
    
    def download_tile_with_progress(self, x, y):
        """Wrapper for download_tile that emits log messages"""
        result = download_tile(self.panorama_id, self.zoom, x, y, self.output_dir)
        return result


class BatchDownloadThread(QThread):
    """Thread for downloading multiple panoramas in batch"""
    progress_signal = pyqtSignal(int, int, str)
    finished_signal = pyqtSignal(bool, str)
    log_signal = pyqtSignal(str)
    item_finished_signal = pyqtSignal(int, bool, str)
    
    def __init__(self, panorama_list, zoom, output_dir, workers, keep_tiles, delay_between=2):
        super().__init__()
        self.panorama_list = panorama_list
        self.zoom = zoom
        self.output_dir = output_dir
        self.workers = workers
        self.keep_tiles = keep_tiles
        self.delay_between = delay_between
        self.is_cancelled = False
        
    def run(self):
        total = len(self.panorama_list)
        success_count = 0
        fail_count = 0
        
        for i, panorama_input in enumerate(self.panorama_list):
            if self.is_cancelled:
                self.log_signal.emit("Batch download cancelled by user")
                self.finished_signal.emit(False, "Batch download cancelled")
                return
                
            self.log_signal.emit(f"Processing item {i+1}/{total}: {panorama_input}")
            self.progress_signal.emit(int(i/total*100), i, f"Processing {i+1}/{total}")
            
            try:
                # Extract panorama ID
                panorama_id, lat, lng = extract_panorama_id_and_coords(panorama_input)
                self.log_signal.emit(f"Extracted panorama ID: {panorama_id}")
                
                # Use the main output directory for all panoramas
                panorama_dir = self.output_dir
                os.makedirs(panorama_dir, exist_ok=True)
                
                # Get tile range
                x_min, x_max, y_min, y_max, total_tiles = get_valid_tile_range(self.zoom)
                
                # Download tiles
                downloaded_count = 0
                with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as executor:
                    futures = []
                    for y in range(y_min, y_max + 1):
                        for x in range(x_min, x_max + 1):
                            futures.append(
                                executor.submit(download_tile, panorama_id, self.zoom, x, y, panorama_dir)
                            )
                    
                    for future in concurrent.futures.as_completed(futures):
                        if future.result():
                            downloaded_count += 1
                
                if downloaded_count > 0:
                    # Stitch panorama
                    stitched_file = stitch_panorama(
                        panorama_id, self.zoom, panorama_dir, 
                        panorama_input, not self.keep_tiles
                    )
                    
                    if stitched_file:
                        self.log_signal.emit(f"Successfully created panorama: {stitched_file}")
                        self.item_finished_signal.emit(i, True, f"Success: {panorama_id}")
                        success_count += 1
                    else:
                        self.log_signal.emit(f"Failed to stitch panorama: {panorama_id}")
                        self.item_finished_signal.emit(i, False, f"Failed to stitch: {panorama_id}")
                        fail_count += 1
                else:
                    self.log_signal.emit(f"No tiles downloaded for: {panorama_id}")
                    self.item_finished_signal.emit(i, False, f"No tiles: {panorama_id}")
                    fail_count += 1
                    
                # Delay between downloads to avoid rate limiting
                if i < total - 1 and self.delay_between > 0:
                    self.log_signal.emit(f"Waiting {self.delay_between} seconds before next download...")
                    for sec in range(self.delay_between):
                        if self.is_cancelled:
                            break
                        time.sleep(1)
                        
            except Exception as e:
                self.log_signal.emit(f"Error processing {panorama_input}: {str(e)}")
                self.item_finished_signal.emit(i, False, f"Error: {str(e)}")
                fail_count += 1
        
        if self.is_cancelled:
            self.finished_signal.emit(False, f"Batch cancelled. Completed {success_count} of {total}")
        else:
            self.finished_signal.emit(True, f"Batch completed. Success: {success_count}, Failed: {fail_count}")
    
    def cancel(self):
        self.is_cancelled = True


class AreaScanThread(QThread):
    """Thread for scanning an area for Street View panoramas using Selenium with multiple threads"""
    progress_signal = pyqtSignal(int, str)
    finished_signal = pyqtSignal(bool, str, list)
    log_signal = pyqtSignal(str)
    
    def __init__(self, top, bottom, left, right, distance_meters, chrome_driver_path, max_threads=4):
        super().__init__()
        self.top = top
        self.bottom = bottom
        self.left = left
        self.right = right
        self.distance_meters = distance_meters
        self.chrome_driver_path = chrome_driver_path
        self.max_threads = max_threads
        self.is_cancelled = False
        self.found_panoramas = []
        self.lock = threading.Lock()  # For thread-safe operations
        
    def run(self):
        try:
            self.log_signal.emit(f"Initializing area scan with {self.max_threads} threads...")
            
            # Convert meters to degrees for scanning
            center_lat = (self.top + self.bottom) / 2
            lat_step, lon_step = meters_to_degrees(self.distance_meters, center_lat)
            
            # Calculate total steps for progress
            lat_steps = int((self.top - self.bottom) / lat_step) + 1
            lon_steps = int((self.right - self.left) / lon_step) + 1
            total_steps = lat_steps * lon_steps
            current_step = 0
            
            self.log_signal.emit(f"Scanning area with {total_steps} points using {self.max_threads} threads...")
            self.log_signal.emit(f"Step size: {lat_step:.6f}° lat, {lon_step:.6f}° lon")
            
            # Create a list of all coordinates to scan
            coordinates_to_scan = []
            lat = self.top
            while lat >= self.bottom and not self.is_cancelled:
                lng = self.left
                while lng <= self.right and not self.is_cancelled:
                    coordinates_to_scan.append((lat, lng))
                    lng += lon_step
                lat -= lat_step
            
            # Use ThreadPoolExecutor for concurrent scanning
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_threads) as executor:
                # Submit all scan tasks
                future_to_coord = {
                    executor.submit(self.check_single_location, coord): coord 
                    for coord in coordinates_to_scan
                }
                
                # Process results as they complete
                for future in concurrent.futures.as_completed(future_to_coord):
                    if self.is_cancelled:
                        break
                        
                    coord = future_to_coord[future]
                    try:
                        panorama_url = future.result()
                        if panorama_url:
                            with self.lock:
                                if panorama_url not in self.found_panoramas:
                                    self.found_panoramas.append(panorama_url)
                                    self.log_signal.emit(f"Found panorama: {panorama_url}")
                    except Exception as e:
                        self.log_signal.emit(f"Error processing location {coord}: {str(e)}")
                    
                    # Update progress
                    current_step += 1
                    progress = int((current_step / total_steps) * 100)
                    self.progress_signal.emit(
                        progress, 
                        f"Processed {current_step}/{total_steps} points"
                    )
            
            if self.is_cancelled:
                self.log_signal.emit("Area scan cancelled by user")
                self.finished_signal.emit(False, "Scan cancelled", [])
            else:
                self.log_signal.emit(f"Scan completed. Found {len(self.found_panoramas)} panoramas.")
                self.finished_signal.emit(True, f"Found {len(self.found_panoramas)} panoramas", self.found_panoramas)
                
        except Exception as e:
            self.log_signal.emit(f"Error during area scan: {str(e)}")
            self.finished_signal.emit(False, f"Error: {str(e)}", [])
    
    def check_single_location(self, coord):
        """Check a single location for Street View panorama (thread-safe)"""
        lat, lng = coord
        
        # Each thread needs its own browser instance
        chrome_options = Options()
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--window-size=1920,1080")
        chrome_options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36")
        
        try:
            from selenium.webdriver.chrome.service import Service
            service = Service(executable_path=self.chrome_driver_path)
            driver = webdriver.Chrome(service=service, options=chrome_options)
            
            try:
                # Use the direct Street View URL approach
                sv_url = f"https://www.google.com/maps/@?api=1&map_action=pano&viewpoint={lat},{lng}"
                driver.get(sv_url)
                
                # Wait for the page to load
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.TAG_NAME, "body"))
                )
                
                # Handle cookie consent if it appears
                current_url = driver.current_url
                if "consent.google.com" in current_url:
                    try:
                        accept_buttons = driver.find_elements(By.XPATH, 
                            "//button[contains(@aria-label, 'Alle akzeptieren') or " +
                            "contains(@aria-label, 'Accept all') or " +
                            "contains(text(), 'Alle akzeptieren') or " +
                            "contains(text(), 'Accept all')]"
                        )
                        
                        if accept_buttons:
                            driver.execute_script("arguments[0].click();", accept_buttons[0])
                            time.sleep(1)
                    except:
                        pass
                
                # If we're still on consent page, try to bypass it
                if "consent.google.com" in driver.current_url:
                    driver.get(sv_url)
                    time.sleep(2)
                
                # Wait for Street View to load
                time.sleep(2)
                
                # Check if Street View is loaded
                panorama_loaded = False
                start_time = time.time()
                timeout = 8  # Reduced timeout for faster scanning
                
                while time.time() - start_time < timeout and not panorama_loaded:
                    current_url = driver.current_url
                    
                    # Check if we have a proper Street View URL with panorama data
                    if ("!1s" in current_url or "panoid=" in current_url or 
                        "data=" in current_url or "pano=" in current_url):
                        panorama_loaded = True
                        return current_url
                    
                    # If we're still on the basic viewpoint URL, wait a bit more
                    elif "viewpoint=" in current_url:
                        time.sleep(0.5)
                    else:
                        break
                
                return None
                    
            finally:
                driver.quit()
                
        except Exception as e:
            self.log_signal.emit(f"Error checking location {lat:.6f}, {lng:.6f}: {str(e)}")
            return None
    
    def cancel(self):
        self.is_cancelled = True


class StreetViewDownloaderGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.download_thread = None
        self.batch_thread = None
        self.area_scan_thread = None
        self.batch_items = []
        self.initUI()
        
    def initUI(self):
        self.setWindowTitle('Google Street View Downloader')
        self.setGeometry(100, 100, 1000, 700)
        
        # Set application style
        QApplication.setStyle(QStyleFactory.create('Fusion'))
        
        # Create dark palette
        dark_palette = QPalette()
        dark_palette.setColor(QPalette.Window, QColor(53, 53, 53))
        dark_palette.setColor(QPalette.WindowText, Qt.white)
        dark_palette.setColor(QPalette.Base, QColor(25, 25, 25))
        dark_palette.setColor(QPalette.AlternateBase, QColor(53, 53, 53))
        dark_palette.setColor(QPalette.ToolTipBase, Qt.white)
        dark_palette.setColor(QPalette.ToolTipText, Qt.white)
        dark_palette.setColor(QPalette.Text, Qt.white)
        dark_palette.setColor(QPalette.Button, QColor(53, 53, 53))
        dark_palette.setColor(QPalette.ButtonText, Qt.white)
        dark_palette.setColor(QPalette.BrightText, Qt.red)
        dark_palette.setColor(QPalette.Link, QColor(42, 130, 218))
        dark_palette.setColor(QPalette.Highlight, QColor(42, 130, 218))
        dark_palette.setColor(QPalette.HighlightedText, Qt.black)
        QApplication.setPalette(dark_palette)
        
        # Central widget
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        # Main layout
        main_layout = QVBoxLayout(central_widget)
        
        # Create tabs
        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)
        
        # Single download tab
        single_tab = QWidget()
        single_layout = QVBoxLayout(single_tab)
        
        # Input group
        input_group = QGroupBox("Panorama Source")
        input_layout = QVBoxLayout(input_group)
        
        url_layout = QHBoxLayout()
        url_label = QLabel("URL or Panorama ID:")
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("Paste Google Maps URL or Panorama ID here")
        url_layout.addWidget(url_label)
        url_layout.addWidget(self.url_input)
        input_layout.addLayout(url_layout)
        
        single_layout.addWidget(input_group)
        
        # Settings group
        settings_group = QGroupBox("Download Settings")
        settings_layout = QVBoxLayout(settings_group)
        
        # Zoom level
        zoom_layout = QHBoxLayout()
        zoom_label = QLabel("Zoom Level:")
        self.zoom_combo = QComboBox()
        self.zoom_combo.addItems(["0 (13×7 tiles)", "1 (26×14 tiles)", "2 (52×28 tiles)", 
                                 "3 (104×56 tiles)", "4 (16×8 tiles)", "5 (32×16 tiles)"])
        self.zoom_combo.setCurrentIndex(4)  # Default to level 4
        zoom_layout.addWidget(zoom_label)
        zoom_layout.addWidget(self.zoom_combo)
        settings_layout.addLayout(zoom_layout)
        
        # Workers
        workers_layout = QHBoxLayout()
        workers_label = QLabel("Concurrent Downloads:")
        self.workers_spin = QSpinBox()
        self.workers_spin.setRange(1, 16)
        self.workers_spin.setValue(4)
        workers_layout.addWidget(workers_label)
        workers_layout.addWidget(self.workers_spin)
        settings_layout.addLayout(workers_layout)
        
        # Output directory
        output_layout = QHBoxLayout()
        output_label = QLabel("Output Directory:")
        self.output_input = QLineEdit()
        self.output_input.setText("./streetview_tiles")
        self.browse_button = QPushButton("Browse")
        self.browse_button.clicked.connect(self.browse_output)
        output_layout.addWidget(output_label)
        output_layout.addWidget(self.output_input)
        output_layout.addWidget(self.browse_button)
        settings_layout.addLayout(output_layout)
        
        # Keep tiles option
        self.keep_tiles_check = QCheckBox("Keep individual tiles after stitching")
        settings_layout.addWidget(self.keep_tiles_check)
        
        single_layout.addWidget(settings_group)
        
        # Progress group
        progress_group = QGroupBox("Progress")
        progress_layout = QVBoxLayout(progress_group)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        progress_layout.addWidget(self.progress_bar)
        
        self.status_label = QLabel("Ready to download")
        progress_layout.addWidget(self.status_label)
        
        single_layout.addWidget(progress_group)
        
        # Buttons
        button_layout = QHBoxLayout()
        self.download_button = QPushButton("Download Panorama")
        self.download_button.clicked.connect(self.start_download)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.cancel_download)
        self.cancel_button.setEnabled(False)
        button_layout.addWidget(self.download_button)
        button_layout.addWidget(self.cancel_button)
        
        single_layout.addLayout(button_layout)
        
        # Batch download tab
        batch_tab = QWidget()
        batch_layout = QVBoxLayout(batch_tab)
        
        # Batch input group
        batch_input_group = QGroupBox("Batch Input")
        batch_input_layout = QVBoxLayout(batch_input_group)
        
        # Batch input methods
        batch_method_layout = QHBoxLayout()
        batch_method_label = QLabel("Input Method:")
        self.batch_method_combo = QComboBox()
        self.batch_method_combo.addItems(["List of URLs/IDs", "CSV File", "JSON File"])
        batch_method_layout.addWidget(batch_method_label)
        batch_method_layout.addWidget(self.batch_method_combo)
        batch_input_layout.addLayout(batch_method_layout)
        
        # Batch input area
        batch_input_area_layout = QHBoxLayout()
        self.batch_text_edit = QTextEdit()
        self.batch_text_edit.setPlaceholderText("Enter one URL or Panorama ID per line")
        batch_input_area_layout.addWidget(self.batch_text_edit)
        
        # Batch buttons
        batch_buttons_layout = QVBoxLayout()
        self.load_batch_button = QPushButton("Load from File")
        self.load_batch_button.clicked.connect(self.load_batch_file)
        self.clear_batch_button = QPushButton("Clear List")
        self.clear_batch_button.clicked.connect(self.clear_batch_list)
        batch_buttons_layout.addWidget(self.load_batch_button)
        batch_buttons_layout.addWidget(self.clear_batch_button)
        batch_buttons_layout.addStretch()
        batch_input_area_layout.addLayout(batch_buttons_layout)
        
        batch_input_layout.addLayout(batch_input_area_layout)
        
        batch_layout.addWidget(batch_input_group)
        
        # Batch settings group
        batch_settings_group = QGroupBox("Batch Settings")
        batch_settings_layout = QVBoxLayout(batch_settings_group)
        
        # Delay between downloads
        delay_layout = QHBoxLayout()
        delay_label = QLabel("Delay between downloads (seconds):")
        self.delay_spin = QSpinBox()
        self.delay_spin.setRange(0, 60)
        self.delay_spin.setValue(2)
        self.delay_spin.setToolTip("Add delay between downloads to avoid rate limiting")
        delay_layout.addWidget(delay_label)
        delay_layout.addWidget(self.delay_spin)
        batch_settings_layout.addLayout(delay_layout)
        
        batch_layout.addWidget(batch_settings_group)
        
        # Batch progress group
        batch_progress_group = QGroupBox("Batch Progress")
        batch_progress_layout = QVBoxLayout(batch_progress_group)
        
        self.batch_progress_bar = QProgressBar()
        self.batch_progress_bar.setValue(0)
        batch_progress_layout.addWidget(self.batch_progress_bar)
        
        self.batch_status_label = QLabel("Ready for batch download")
        batch_progress_layout.addWidget(self.batch_status_label)
        
        # Batch results table
        self.batch_table = QTableWidget()
        self.batch_table.setColumnCount(3)
        self.batch_table.setHorizontalHeaderLabels(["URL/ID", "Status", "Message"])
        self.batch_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.batch_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.batch_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        batch_progress_layout.addWidget(self.batch_table)
        
        batch_layout.addWidget(batch_progress_group)
        
        # Batch buttons
        batch_button_layout = QHBoxLayout()
        self.start_batch_button = QPushButton("Start Batch Download")
        self.start_batch_button.clicked.connect(self.start_batch_download)
        self.cancel_batch_button = QPushButton("Cancel Batch")
        self.cancel_batch_button.clicked.connect(self.cancel_batch_download)
        self.cancel_batch_button.setEnabled(False)
        batch_button_layout.addWidget(self.start_batch_button)
        batch_button_layout.addWidget(self.cancel_batch_button)
        
        batch_layout.addLayout(batch_button_layout)
        
        # Area Scan tab
        area_scan_tab = QWidget()
        area_scan_layout = QVBoxLayout(area_scan_tab)
        
        # Area definition group
        area_group = QGroupBox("Area Definition")
        area_layout = QGridLayout(area_group)  # This line should come FIRST
        
        # Coordinate inputs
        area_layout.addWidget(QLabel("Top (North) Latitude:"), 0, 0)
        self.top_lat_input = QDoubleSpinBox()
        self.top_lat_input.setRange(-90, 90)
        self.top_lat_input.setDecimals(15)
        self.top_lat_input.setValue(40.7589)
        area_layout.addWidget(self.top_lat_input, 0, 1)
        
        area_layout.addWidget(QLabel("Bottom (South) Latitude:"), 1, 0)
        self.bottom_lat_input = QDoubleSpinBox()
        self.bottom_lat_input.setRange(-90, 90)
        self.bottom_lat_input.setDecimals(15)
        self.bottom_lat_input.setValue(40.7489)
        area_layout.addWidget(self.bottom_lat_input, 1, 1)
        
        area_layout.addWidget(QLabel("Left (West) Longitude:"), 2, 0)
        self.left_lng_input = QDoubleSpinBox()
        self.left_lng_input.setRange(-180, 180)
        self.left_lng_input.setDecimals(15)
        self.left_lng_input.setValue(-73.9857)
        area_layout.addWidget(self.left_lng_input, 2, 1)
        
        area_layout.addWidget(QLabel("Right (East) Longitude:"), 3, 0)
        self.right_lng_input = QDoubleSpinBox()
        self.right_lng_input.setRange(-180, 180)
        self.right_lng_input.setDecimals(15)
        self.right_lng_input.setValue(-73.9757)
        area_layout.addWidget(self.right_lng_input, 3, 1)
        
        area_layout.addWidget(QLabel("Scan Distance (meters):"), 4, 0)
        self.distance_input = QSpinBox()
        self.distance_input.setRange(1, 1000)
        self.distance_input.setValue(50)
        self.distance_input.setSuffix(" meters")
        self.distance_input.setToolTip("Distance between scan points in meters")
        area_layout.addWidget(self.distance_input, 4, 1)
        
        area_layout.addWidget(QLabel("Chrome Driver Path:"), 5, 0)
        chrome_path_layout = QHBoxLayout()
        self.chrome_driver_input = QLineEdit()
        self.chrome_driver_input.setPlaceholderText("Path to chromedriver executable")
        self.browse_chrome_button = QPushButton("Browse")
        self.browse_chrome_button.clicked.connect(self.browse_chrome_driver)
        chrome_path_layout.addWidget(self.chrome_driver_input)
        chrome_path_layout.addWidget(self.browse_chrome_button)
        area_layout.addLayout(chrome_path_layout, 5, 1)
        
        # Add the Scan Threads widget AFTER area_layout has been created
        area_layout.addWidget(QLabel("Scan Threads:"), 6, 0)
        self.threads_input = QSpinBox()
        self.threads_input.setRange(1, 16)
        self.threads_input.setValue(4)
        self.threads_input.setToolTip("Number of concurrent threads for scanning")
        area_layout.addWidget(self.threads_input, 6, 1)
        
        area_scan_layout.addWidget(area_group)
        
        # Scan progress group
        scan_progress_group = QGroupBox("Scan Progress")
        scan_progress_layout = QVBoxLayout(scan_progress_group)
        
        self.scan_progress_bar = QProgressBar()
        self.scan_progress_bar.setValue(0)
        scan_progress_layout.addWidget(self.scan_progress_bar)
        
        self.scan_status_label = QLabel("Ready to scan area")
        scan_progress_layout.addWidget(self.scan_status_label)
        
        # Found panoramas list
        self.found_panoramas_list = QListWidget()
        scan_progress_layout.addWidget(QLabel("Found Panoramas:"))
        scan_progress_layout.addWidget(self.found_panoramas_list)
        
        area_scan_layout.addWidget(scan_progress_group)
        
        # Scan buttons
        scan_button_layout = QHBoxLayout()
        self.start_scan_button = QPushButton("Start Area Scan")
        self.start_scan_button.clicked.connect(self.start_area_scan)
        self.cancel_scan_button = QPushButton("Cancel Scan")
        self.cancel_scan_button.clicked.connect(self.cancel_area_scan)
        self.cancel_scan_button.setEnabled(False)
        self.add_to_batch_button = QPushButton("Add to Batch")
        self.add_to_batch_button.clicked.connect(self.add_found_to_batch)
        self.add_to_batch_button.setEnabled(False)
        scan_button_layout.addWidget(self.start_scan_button)
        scan_button_layout.addWidget(self.cancel_scan_button)
        scan_button_layout.addWidget(self.add_to_batch_button)
        
        area_scan_layout.addLayout(scan_button_layout)
        
        # Log tab
        log_tab = QWidget()
        log_layout = QVBoxLayout(log_tab)
        
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        log_layout.addWidget(self.log_output)
        
        # Add tabs
        self.tabs.addTab(single_tab, "Single Download")
        self.tabs.addTab(batch_tab, "Batch Download")
        self.tabs.addTab(area_scan_tab, "Area Scan")
        self.tabs.addTab(log_tab, "Log")
        
        # Set font
        font = QFont("Consolas", 9)
        self.log_output.setFont(font)
        self.batch_text_edit.setFont(font)
        
        self.log_message("Application started. Ready to download panoramas.")
        
    def browse_output(self):
        directory = QFileDialog.getExistingDirectory(self, "Select Output Directory")
        if directory:
            self.output_input.setText(directory)
            
    def browse_chrome_driver(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Select ChromeDriver Executable", "", "Executable Files (*.exe);;All Files (*)")
        if file_path:
            self.chrome_driver_input.setText(file_path)
            
    def log_message(self, message):
        self.log_output.append(message)
        self.log_output.ensureCursorVisible()
        
    def start_download(self):
        panorama_input = self.url_input.text().strip()
        if not panorama_input:
            QMessageBox.warning(self, "Input Error", "Please enter a Google Maps URL or Panorama ID")
            return
            
        zoom_level = self.zoom_combo.currentIndex()
        output_dir = self.output_input.text().strip()
        workers = self.workers_spin.value()
        keep_tiles = self.keep_tiles_check.isChecked()
        
        self.download_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress_bar.setValue(0)
        
        self.log_message(f"Starting download for: {panorama_input}")
        self.log_message(f"Zoom level: {zoom_level}, Workers: {workers}")
        self.log_message(f"Output directory: {output_dir}")
        
        self.download_thread = DownloadThread(panorama_input, zoom_level, output_dir, workers, keep_tiles)
        self.download_thread.progress_signal.connect(self.update_progress)
        self.download_thread.finished_signal.connect(self.download_finished)
        self.download_thread.log_signal.connect(self.log_message)
        self.download_thread.start()
        
    def cancel_download(self):
        if self.download_thread and self.download_thread.isRunning():
            self.download_thread.terminate()
            self.download_thread.wait()
            self.log_message("Download cancelled by user")
            self.reset_ui()
            
    def update_progress(self, progress, downloaded, status):
        self.progress_bar.setValue(progress)
        self.status_label.setText(f"{status} - {downloaded} tiles downloaded")
        
    def download_finished(self, success, message):
        if success:
            self.status_label.setText("Download completed successfully")
            QMessageBox.information(self, "Success", message)
        else:
            self.status_label.setText("Download failed")
            QMessageBox.warning(self, "Error", message)
            
        self.reset_ui()
        
    def reset_ui(self):
        self.download_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        
    def load_batch_file(self):
        method = self.batch_method_combo.currentIndex()
        if method == 0:  # List of URLs/IDs
            file_path, _ = QFileDialog.getOpenFileName(self, "Open Text File", "", "Text Files (*.txt);;All Files (*)")
            if file_path:
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                        self.batch_text_edit.setPlainText(content)
                    self.log_message(f"Loaded batch from: {file_path}")
                except Exception as e:
                    QMessageBox.warning(self, "Error", f"Failed to load file: {str(e)}")
        elif method == 1:  # CSV
            file_path, _ = QFileDialog.getOpenFileName(self, "Open CSV File", "", "CSV Files (*.csv);;All Files (*)")
            if file_path:
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        reader = csv.reader(f)
                        urls = []
                        for row in reader:
                            if row and row[0].strip():
                                urls.append(row[0].strip())
                        self.batch_text_edit.setPlainText("\n".join(urls))
                    self.log_message(f"Loaded {len(urls)} URLs from CSV: {file_path}")
                except Exception as e:
                    QMessageBox.warning(self, "Error", f"Failed to load CSV: {str(e)}")
        elif method == 2:  # JSON
            file_path, _ = QFileDialog.getOpenFileName(self, "Open JSON File", "", "JSON Files (*.json);;All Files (*)")
            if file_path:
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        if isinstance(data, list):
                            urls = [str(item) for item in data if str(item).strip()]
                            self.batch_text_edit.setPlainText("\n".join(urls))
                        else:
                            QMessageBox.warning(self, "Error", "JSON file should contain an array of URLs/IDs")
                    self.log_message(f"Loaded JSON from: {file_path}")
                except Exception as e:
                    QMessageBox.warning(self, "Error", f"Failed to load JSON: {str(e)}")
    
    def clear_batch_list(self):
        self.batch_text_edit.clear()
        self.batch_table.setRowCount(0)
        self.log_message("Cleared batch list")
        
    def start_batch_download(self):
        batch_text = self.batch_text_edit.toPlainText().strip()
        if not batch_text:
            QMessageBox.warning(self, "Input Error", "Please enter at least one URL or Panorama ID")
            return
            
        zoom_level = self.zoom_combo.currentIndex()
        output_dir = self.output_input.text().strip()
        workers = self.workers_spin.value()
        keep_tiles = self.keep_tiles_check.isChecked()
        delay = self.delay_spin.value()
        
        # Parse batch items
        self.batch_items = [line.strip() for line in batch_text.split('\n') if line.strip()]
        
        if not self.batch_items:
            QMessageBox.warning(self, "Input Error", "No valid URLs or IDs found")
            return
            
        # Setup results table
        self.batch_table.setRowCount(len(self.batch_items))
        for i, item in enumerate(self.batch_items):
            self.batch_table.setItem(i, 0, QTableWidgetItem(item[:100] + "..." if len(item) > 100 else item))
            self.batch_table.setItem(i, 1, QTableWidgetItem("Pending"))
            self.batch_table.setItem(i, 2, QTableWidgetItem(""))
        
        self.start_batch_button.setEnabled(False)
        self.cancel_batch_button.setEnabled(True)
        self.batch_progress_bar.setValue(0)
        
        self.log_message(f"Starting batch download of {len(self.batch_items)} panoramas")
        self.log_message(f"Zoom level: {zoom_level}, Workers: {workers}, Delay: {delay}s")
        self.log_message(f"Output directory: {output_dir}")
        
        self.batch_thread = BatchDownloadThread(
            self.batch_items, zoom_level, output_dir, workers, keep_tiles, delay
        )
        self.batch_thread.progress_signal.connect(self.update_batch_progress)
        self.batch_thread.finished_signal.connect(self.batch_finished)
        self.batch_thread.log_signal.connect(self.log_message)
        self.batch_thread.item_finished_signal.connect(self.update_batch_item_status)
        self.batch_thread.start()
        
    def cancel_batch_download(self):
        if self.batch_thread and self.batch_thread.isRunning():
            self.batch_thread.cancel()
            self.batch_thread.wait()
            self.log_message("Batch download cancelled by user")
            self.reset_batch_ui()
            
    def update_batch_progress(self, progress, completed, status):
        self.batch_progress_bar.setValue(progress)
        self.batch_status_label.setText(f"{status} - {completed}/{len(self.batch_items)} completed")
        
    def update_batch_item_status(self, index, success, message):
        status = "Success" if success else "Failed"
        self.batch_table.setItem(index, 1, QTableWidgetItem(status))
        self.batch_table.setItem(index, 2, QTableWidgetItem(message))
        
        # Color code the row
        for col in range(3):
            item = self.batch_table.item(index, col)
            if item:
                if success:
                    item.setBackground(QColor(50, 120, 50))  # Dark green for success
                else:
                    item.setBackground(QColor(120, 50, 50))  # Dark red for failure
        
    def batch_finished(self, success, message):
        if success:
            self.batch_status_label.setText("Batch completed successfully")
            QMessageBox.information(self, "Success", message)
        else:
            self.batch_status_label.setText("Batch completed with errors")
            QMessageBox.warning(self, "Completed", message)
            
        self.reset_batch_ui()
        
    def reset_batch_ui(self):
        self.start_batch_button.setEnabled(True)
        self.cancel_batch_button.setEnabled(False)
        
    def start_area_scan(self):
        # Get area parameters
        top = self.top_lat_input.value()
        bottom = self.bottom_lat_input.value()
        left = self.left_lng_input.value()
        right = self.right_lng_input.value()
        distance_meters = self.distance_input.value()
        chrome_driver_path = self.chrome_driver_input.text().strip()
        max_threads = self.threads_input.value()  # Get thread count
        
        # Validate inputs
        if top <= bottom:
            QMessageBox.warning(self, "Input Error", "Top latitude must be greater than bottom latitude")
            return
            
        if left >= right:
            QMessageBox.warning(self, "Input Error", "Left longitude must be less than right longitude")
            return
            
        if distance_meters <= 0:
            QMessageBox.warning(self, "Input Error", "Distance must be greater than 0")
            return
            
        if not chrome_driver_path or not os.path.exists(chrome_driver_path):
            QMessageBox.warning(self, "Input Error", "Please provide a valid path to ChromeDriver")
            return
        
        # Convert meters to degrees for scanning
        center_lat = (top + bottom) / 2
        lat_degrees, lon_degrees = meters_to_degrees(distance_meters, center_lat)
        
        # Calculate approximate number of points
        lat_points = int((top - bottom) / lat_degrees) + 1
        lon_points = int((right - left) / lon_degrees) + 1
        total_points = lat_points * lon_points
        
        reply = QMessageBox.question(
            self, "Confirm Area Scan", 
            f"This will scan approximately {total_points} points.\nDistance: {distance_meters} meters\n\nContinue?",
            QMessageBox.Yes | QMessageBox.No
        )
        
        if reply != QMessageBox.Yes:
            return
        
        # Clear previous results
        self.found_panoramas_list.clear()
        
        # Start the scan with distance in meters
        self.start_scan_button.setEnabled(False)
        self.cancel_scan_button.setEnabled(True)
        self.add_to_batch_button.setEnabled(False)
        self.scan_progress_bar.setValue(0)
        
        self.log_message(f"Starting area scan from ({top:.6f}, {left:.6f}) to ({bottom:.6f}, {right:.6f})")
        self.log_message(f"Scan distance: {distance_meters} meters")
        self.log_message(f"Approximate step size: {lat_degrees:.6f}° lat, {lon_degrees:.6f}° lon")
        self.log_message(f"Estimated points: {total_points}")
        
        # Pass the distance in meters to the thread
        max_threads = self.threads_input.value()  # Get thread count
        self.area_scan_thread = AreaScanThread(
            top, bottom, left, right, distance_meters, chrome_driver_path, max_threads
        )
        self.area_scan_thread.progress_signal.connect(self.update_scan_progress)
        self.area_scan_thread.finished_signal.connect(self.area_scan_finished)
        self.area_scan_thread.log_signal.connect(self.log_message)
        self.area_scan_thread.start()
            
    def cancel_area_scan(self):
        if self.area_scan_thread and self.area_scan_thread.isRunning():
            self.area_scan_thread.cancel()
            self.area_scan_thread.wait()
            self.log_message("Area scan cancelled by user")
            self.reset_scan_ui()
            
    def update_scan_progress(self, progress, status):
        self.scan_progress_bar.setValue(progress)
        self.scan_status_label.setText(status)
        
    def area_scan_finished(self, success, message, panoramas):
        if success:
            self.scan_status_label.setText("Scan completed successfully")
            self.log_message(f"Area scan completed: {message}")
            
            # Add found panoramas to the list
            for panorama in panoramas:
                self.found_panoramas_list.addItem(panorama)
                
            self.add_to_batch_button.setEnabled(len(panoramas) > 0)
        else:
            self.scan_status_label.setText("Scan failed")
            self.log_message(f"Area scan failed: {message}")
            
        self.reset_scan_ui()
        
    def reset_scan_ui(self):
        self.start_scan_button.setEnabled(True)
        self.cancel_scan_button.setEnabled(False)
        
    def add_found_to_batch(self):
        # Get all items from the found panoramas list
        count = self.found_panoramas_list.count()
        if count == 0:
            return
            
        # Add to batch text area
        current_batch = self.batch_text_edit.toPlainText().strip()
        new_items = []
        
        for i in range(count):
            item = self.found_panoramas_list.item(i)
            new_items.append(item.text())
            
        if current_batch:
            new_text = current_batch + "\n" + "\n".join(new_items)
        else:
            new_text = "\n".join(new_items)
            
        self.batch_text_edit.setPlainText(new_text)
        
        # Switch to batch tab
        self.tabs.setCurrentIndex(1)  # Batch tab is index 1
        
        self.log_message(f"Added {count} panoramas to batch download list")

def main():
    # If command line arguments are provided, use the CLI version
    if len(sys.argv) > 1:
        # Original CLI code
        parser = argparse.ArgumentParser(
            description="Download and stitch Google Street View panorama tiles",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="""
    Examples:
      python streetview_downloader.py "URL" --zoom 4 --output ./panorama
      python streetview_downloader.py XTIo-CsHeK2YHDEBb5gLQA --zoom 2 --output ./my_pano --keep-tiles

    Zoom level guide:
      0: 13×7 tiles (6656×3584 pixels)
      1: 26×14 tiles (13312×7168 pixels)
      2: 52×28 tiles (26624×14336 pixels)
      3: 104×56 tiles (53248×28672 pixels)
      4: 16×8 tiles (8192×4096 pixels)
      5: 32×16 tiles (16384×8192 pixels)
            """
        )
        
        parser.add_argument("panorama_input", help="Panorama ID or Google Maps URL")
        parser.add_argument("--zoom", "-z", type=int, default=4, 
                           help="Zoom level (0-5, default: 4)")
        parser.add_argument("--output", "-o", default="./streetview_tiles",
                           help="Output directory (default: ./streetview_tiles)")
        parser.add_argument("--workers", "-w", type=int, default=4,
                           help="Number of concurrent downloads (default: 4)")
        parser.add_argument("--stitch-only", action="store_true",
                           help="Only stitch existing tiles (don't download)")
        parser.add_argument("--keep-tiles", action="store_true",
                           help="Keep tile files after stitching (don't delete)")
        
        args = parser.parse_args()
        
        try:
            # Extract both ID and coordinates from input
            panorama_id, lat, lng = extract_panorama_id_and_coords(args.panorama_input)
            print(f"Extracted panorama ID: {panorama_id}")
            if lat is not None and lng is not None:
                print(f"Extracted coordinates: {lat:.8f}, {lng:.8f}")
            
            if args.stitch_only:
                if not os.path.exists(args.output):
                    print(f"Error: Output directory '{args.output}' does not exist")
                    return 1
                
                print("Stitching existing tiles...")
                stitched_file = stitch_panorama(panorama_id, args.zoom, args.output, 
                                              args.panorama_input, not args.keep_tiles)
                if stitched_file:
                    print(f"Successfully created panorama: {stitched_file}")
                else:
                    print("Failed to stitch panorama")
                    return 1
            else:
                downloaded_count = download_panorama_tiles(panorama_id, args.zoom, args.output, args.workers)
                
                if downloaded_count > 0:
                    print("Stitching tiles...")
                    stitched_file = stitch_panorama(panorama_id, args.zoom, args.output, 
                                                  args.panorama_input, not args.keep_tiles)
                    if stitched_file:
                        print(f"Successfully created panorama: {stitched_file}")
                    else:
                        print("Failed to stitch panorama")
                        return 1
                else:
                    print("No tiles were downloaded!")
                    return 1
                
        except ValueError as e:
            print(f"Error: {e}")
            return 1
        except Exception as e:
            print(f"Unexpected error: {e}")
            return 1
        
        return 0
    else:
        # Launch GUI if no command line arguments
        app = QApplication(sys.argv)
        
        # Set window icon if available
        if hasattr(sys, '_MEIPASS'):
            # PyInstaller creates a temp folder and stores path in _MEIPASS
            icon_path = os.path.join(sys._MEIPASS, 'icon.ico')
        else:
            icon_path = 'icon.ico' if os.path.exists('icon.ico') else None
            
        if icon_path and os.path.exists(icon_path):
            app.setWindowIcon(QIcon(icon_path))
        
        window = StreetViewDownloaderGUI()
        window.show()
        return app.exec_()


if __name__ == "__main__":
    sys.exit(main())