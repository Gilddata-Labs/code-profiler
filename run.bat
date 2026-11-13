@echo off
REM Runs the profiler on Windows using the settings in .env.
REM   run.bat          full run
REM   run.bat check    only test the token and access
setlocal EnableDelayedExpansion
cd /d "%~dp0"

if exist .env (
  for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do (
    if not "%%A"=="" set "%%A=%%B"
  )
)

set PY=py -3
%PY% --version >nul 2>&1 || set PY=python
%PY% --version >nul 2>&1 || (
  echo Python 3.9 or newer was not found. Install it from python.org or use Docker, see README.
  pause & exit /b 1
)
where git >nul 2>&1 || (echo git was not found. Install Git for Windows and try again. & pause & exit /b 1)

if "%PLATFORM%"=="" set PLATFORM=github
if "%WORKERS%"=="" set WORKERS=4
if "%OUTPUT_DIR%"=="" set OUTPUT_DIR=.\output

if /i "%~1"=="check" goto :check

set ARGS=
if not "%LOCAL_DIR%"=="" (
  set ARGS=--local "%LOCAL_DIR%"
) else (
  if /i "%PLATFORM%"=="github"    set TOKEN=%GITHUB_TOKEN%
  if /i "%PLATFORM%"=="gitlab"    set TOKEN=%GITLAB_TOKEN%
  if /i "%PLATFORM%"=="bitbucket" set TOKEN=%BITBUCKET_TOKEN%
  if "!TOKEN!"=="" (echo The token for %PLATFORM% is empty. Fill in .env, see .env.example. & pause & exit /b 1)
  if "%ORG%"=="" if "%REPOS%"=="" (echo Set ORG or REPOS or LOCAL_DIR in .env. & pause & exit /b 1)
  set ARGS=--platform %PLATFORM% --token !TOKEN!
  if not "%ORG%"=="" set ARGS=!ARGS! --org %ORG%
  if not "%REPOS%"=="" (
    for %%R in (%REPOS%) do (
      if /i "%PLATFORM%"=="github"    set ARGS=!ARGS! --github-repo %%R
      if /i "%PLATFORM%"=="gitlab"    set ARGS=!ARGS! --gitlab-project %%R
      if /i "%PLATFORM%"=="bitbucket" set ARGS=!ARGS! --bitbucket-repo %%R
    )
  )
  if /i "%PLATFORM%"=="bitbucket" if not "%BITBUCKET_EMAIL%"=="" set ARGS=!ARGS! --bitbucket-email %BITBUCKET_EMAIL%
  if not "%GITHUB_HOST%"=="" set ARGS=!ARGS! --github-host %GITHUB_HOST%
  if not "%GITLAB_HOST%"=="" set ARGS=!ARGS! --gitlab-host %GITLAB_HOST%
)
if /i "%INCLUDE_ARCHIVED%"=="true" set ARGS=!ARGS! --include-archived
if /i "%INCLUDE_FORKS%"=="true" set ARGS=!ARGS! --include-forks
set ARGS=!ARGS! --workers %WORKERS% --out "%OUTPUT_DIR%" --keep-work
if not "%LABEL%"=="" set ARGS=!ARGS! --label %LABEL%
if not "%MAX_PRS%"=="" set ARGS=!ARGS! --max-prs %MAX_PRS%
if not "%EXTRA_ARGS%"=="" set ARGS=!ARGS! %EXTRA_ARGS%

echo Profiling with workers=%WORKERS%, output in %OUTPUT_DIR%
%PY% gilddata_profiler.py !ARGS! %*
pause
exit /b %errorlevel%

:check
set CARGS=
if /i "%PLATFORM%"=="github"    set CARGS=--github-token %GITHUB_TOKEN% --github-org %ORG%
if /i "%PLATFORM%"=="gitlab"    set CARGS=--gitlab-token %GITLAB_TOKEN% --gitlab-group %ORG%
if /i "%PLATFORM%"=="bitbucket" set CARGS=--bitbucket-token %BITBUCKET_TOKEN% --bitbucket-email %BITBUCKET_EMAIL% --bitbucket-workspace %ORG%
if not "%GITHUB_HOST%"=="" set CARGS=!CARGS! --github-host %GITHUB_HOST%
if not "%GITLAB_HOST%"=="" set CARGS=!CARGS! --gitlab-host %GITLAB_HOST%
%PY% gilddata_profiler.py check !CARGS!
pause
exit /b %errorlevel%
