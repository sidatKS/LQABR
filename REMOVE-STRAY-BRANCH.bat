@echo off
setlocal
cd /d "%~dp0"
title LQABR - remove the stray LQABR-SP1-agent-gateway branch

echo ==============================================================
echo  LQABR cleanup
echo    - deletes the stray branch LQABR-SP1-agent-gateway
echo    - leaves you checked out on leadq-dev-SN
echo    - does NOT push anything
echo ==============================================================
echo.

echo [1/5] Removing stale git lock files...
if exist ".git\index.lock" del /f /q ".git\index.lock"
if exist ".git\HEAD.lock" del /f /q ".git\HEAD.lock"
if exist ".git\packed-refs.lock" del /f /q ".git\packed-refs.lock"
if exist ".git\refs\heads\LQABR-SP1-agent-gateway.lock" del /f /q ".git\refs\heads\LQABR-SP1-agent-gateway.lock"
echo       done.
echo.

echo [2/5] Stashing working-tree changes so the branch switch can proceed.
echo       That is 41 files of CRLF line-ending noise, plus your real edits to
echo       agents\enrichment\.env.example and requirements.txt.
echo       Nothing is lost - recover them later with:
echo           git stash list
echo           git stash branch enrichment-mcp-env stash@{0}
echo.
git stash push -m "pre-gateway: CRLF noise + enrichment .env/requirements edits"
echo.

echo [3/5] Switching to leadq-dev-SN...
git checkout leadq-dev-SN
if errorlevel 1 (
  echo.
  echo   *** checkout FAILED - stopping here. ***
  echo   *** The stray branch has NOT been deleted, and nothing is lost: ***
  echo   *** the Agent Gateway is already committed on leadq-dev-SN.     ***
  echo.
  pause
  exit /b 1
)
echo.

echo [4/5] Deleting the stray branch LQABR-SP1-agent-gateway...
git branch -D LQABR-SP1-agent-gateway
echo.

echo [5/5] Clearing leftover git temp objects...
git gc --prune=now
echo.

echo ==============================================================
echo  Result
echo ==============================================================
echo.
echo -- branches --
git branch
echo.
echo -- last 3 commits on leadq-dev-SN --
git log --oneline -3
echo.
echo -- working tree --
git status --short
echo.
echo -- the gateway, as committed --
git ls-tree -r --name-only HEAD -- agents/gateway
echo.
echo Expect: no LQABR-SP1-agent-gateway branch, HEAD on leadq-dev-SN,
echo 25 files under agents/gateway, and nothing pushed.
echo.
echo Press any key to close. This script deletes itself.
pause >nul
(goto) 2>nul & del "%~f0"
