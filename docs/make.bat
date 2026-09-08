@ECHO OFF
pushd %~dp0
set SPHINXBUILD=sphinx-build
set SOURCEDIR=source
set BUILDDIR=build
%SPHINXBUILD% >NUL 2>NUL
if errorlevel 9009 (
	echo.Sphinx not found. Install it with: pip install -r requirements.txt
	exit /b 1
)
%SPHINXBUILD% -M %1 %SOURCEDIR% %BUILDDIR% %SPHINXOPTS% %O%
goto end
:end
popd
