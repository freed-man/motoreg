# motoreg

A personal UK vehicle lookup. Enter a reg to see the DVLA record, tax and MOT status, MOT history from DVSA with mileage checks, and an estimated VED rate.

Built from the lookup parts of the auto:commit course project ([freed-man/mp4-autocommit](https://github.com/freed-man/mp4-autocommit)). Everything else (accounts, services, basket, checkout, contact, profiles) was left behind.

Every vehicle has its own address, so `/AB12CDE` works straight from the address bar. Nothing is stored: there is no database, and lookups are not logged.

## Run it locally (PowerShell)

```powershell
C:\Python313\python.exe -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements.txt
python manage.py runserver
```

It needs an `env.py` in the project root (gitignored). The quickest route is to copy the one from mp4-autocommit, because the variable names are the same. Its Stripe, Cloudinary, email and `DATABASE_URL` lines are no longer used.

## Config vars

| Name | What it's for |
| --- | --- |
| `SECRET_KEY` | Django secret key. Make a new one for this app (command below). |
| `DEVELOPMENT` | `True` in env.py only. Leave it off on Heroku so debug mode stays off. |
| `DVLA_API_URL`, `DVLA_API_KEY` | DVLA Vehicle Enquiry Service |
| `MOT_TOKEN_URL`, `MOT_CLIENT_ID`, `MOT_CLIENT_SECRET`, `MOT_SCOPE`, `MOT_API_BASE`, `MOT_API_KEY` | DVSA MOT History API |
| `SITE_PASSWORD` | Optional. Locks the whole site behind one password, asked once per browser and remembered for 90 days. |
| `EXTRA_HOSTS` | Optional. Extra hostnames, comma separated, for example a custom domain. |

New secret key:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(50))"
```

## Deploy to Heroku

1. Push this folder to a new GitHub repo.
2. Create a new Heroku app and connect the repo under Deploy.
3. Add the config vars. The `DVLA_` and `MOT_` values can be copied from the old app's Settings, Config Vars.
4. Deploy the main branch. No database or add-ons are needed, and Heroku runs collectstatic itself.

## Changing the name

`SITE_NAME` in `config/settings.py` is the only place the name appears.

## Tests

```powershell
python manage.py test
```

The API calls are mocked and no database is needed.

## Upkeep

- VED rates change every April. Update `lookup/tax_rates.py` from the new V149 (the note at the top of that file has the link).
- `requirements.txt` is UTF-8. Edit it by hand rather than regenerating it with `pip freeze >` in Windows PowerShell 5.1, which writes UTF-16. That encoding is what broke a Heroku deploy of the old project.

## Where things are

| Path | What it does |
| --- | --- |
| `lookup/services.py` | DVLA and DVSA calls. Both run at once, with 10 second timeouts and a cached DVSA token. |
| `lookup/details.py` | Turns the two API responses into what the result page shows. |
| `lookup/views.py` | Home page, form handler, result page, unlock page. |
| `lookup/middleware.py` | The optional `SITE_PASSWORD` lock. |
| `lookup/tax_rates.py` | VED rate tables, copied from mp4-autocommit. |
| `config/settings.py` | Settings, including `SITE_NAME`. |
