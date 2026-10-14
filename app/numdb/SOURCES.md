# Number data

- `phone.dat` — mainland mobile number ranges (first 7 digits → province, city,
  area code, carrier). Version 2502 (2025-02), 517,259 entries. From
  https://github.com/kurisu994/phone_data (commit 6f9377f617b8, MIT), same
  format as xluohome/phonedata.
  sha256 5858836c6a472a706a690a55419d589ccbcd9e72721852642280d9e10e379bc2
- `areacodes.json` — mainland landline area codes → province/city, built from
  `district.csv` of https://github.com/eduosi/district (Apache-2.0).
- Japanese prefixes, country codes and the service-number names are written
  by hand in `numinfo.py`.
