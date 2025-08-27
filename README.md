# BulkSVD
BulkSVD is a bulk downloader for Google Street View panoramas, including advanced Area Scan features for automated fetching.

Features:
- Single panorama download with a URL or panorama ID
- Batch download for fetching multiple panoramas at once
- Area Scan for automatically fetching panoramas from a specified area
- Custom zoom levels (resolutions), up to 16656x8328


  # Single Panorama Download
  ![Single Panorama Download](https://github.com/pbz134/BulkSVD/blob/main/images/Single.PNG)

  # Batch Download
  ![Batch Download](https://github.com/pbz134/BulkSVD/blob/main/images/Area.PNG)

  # Area Scanning
 ![Area Scanning](https://github.com/pbz134/BulkSVD/blob/main/images/Batch.PNG)


# Prerequisites
- Python 3.10 or higher
- Have Google Chrome installed on your system
- chromedriver.exe: https://googlechromelabs.github.io/chrome-for-testing/


  # Tutorial
- Drag Pegman onto any area to access a panorama
- Copy the browser URL into the downloader
OR
- Copy the top left and bottom right coordinates of your desired area by right-clicking on Google Maps
- Replace dots with comma
- Insert the coordinates accordingly:
![Coordinate Tutorial](https://github.com/pbz134/BulkSVD/blob/main/images/Shirakawa.PNG)


# To Do
- Remove `Error: 'NoneType' object has no attribute 'startswith'` error, aka incorrect panorama links
- Improve Area Scanning process


Be mindful of IP bans! Do not download panoramas with more than one instance at once!
