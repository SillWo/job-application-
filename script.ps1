$ErrorActionPreference = "Stop"

$BadCommit1 = "2b1e6ae128c88c28cb17d9f0732a5eb16b6d2afa"
$BadCommit2 = "3fe655b1575390fc4b8ca6fe3769af614858f5ea"

# ------------------------------------------------------------
# Проверяем, что мы внутри Git-репозитория
# ------------------------------------------------------------

git rev-parse --is-inside-work-tree *> $null

if ($LASTEXITCODE -ne 0) {
    throw "Запусти скрипт из папки Git-проекта."
}

$OriginUrl = (git remote get-url origin).Trim()

if (-not $OriginUrl) {
    throw "Не найден remote 'origin'."
}

# Проверяем git-filter-repo
git filter-repo --version *> $null

if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "Не найден git-filter-repo."
    Write-Host ""
    Write-Host "Установи:"
    Write-Host "  py -m pip install git-filter-repo"
    Write-Host ""
    Write-Host "После этого снова запусти скрипт."
    exit 1
}

Write-Host ""
Write-Host "Remote:"
Write-Host "  $OriginUrl"
Write-Host ""
Write-Host "Будут удалены коммиты:"
Write-Host "  $BadCommit1"
Write-Host "  $BadCommit2"
Write-Host ""
Write-Host "ВНИМАНИЕ: история всех веток и тегов будет переписана."
Write-Host "SHA всех потомков этих коммитов изменятся."
Write-Host ""

$Confirmation = Read-Host "Для продолжения введи DELETE"

if ($Confirmation -ne "DELETE") {
    Write-Host "Отменено."
    exit
}

# ------------------------------------------------------------
# Получаем API key
# ------------------------------------------------------------

Write-Host ""
Write-Host "Введи опубликованный API key."
Write-Host "На экране он отображаться не будет."
Write-Host ""

$SecureKey = Read-Host "API key" -AsSecureString

$Bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureKey)

try {
    $ApiKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Bstr)
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Bstr)
}

if ([string]::IsNullOrWhiteSpace($ApiKey)) {
    throw "API key пустой."
}

# ------------------------------------------------------------
# Создаём временную папку
# ------------------------------------------------------------

$WorkDir = Join-Path `
    ([IO.Path]::GetTempPath()) `
    ("git-secret-clean-" + [guid]::NewGuid().ToString())

$RepoDir = Join-Path $WorkDir "repository.git"
$ReplaceFile = Join-Path $WorkDir "replace.txt"
$SecretFile = Join-Path $WorkDir "secret.txt"

New-Item -ItemType Directory -Path $WorkDir | Out-Null

try {

    # Записываем ключ без BOM и без перевода строки
    $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

    [IO.File]::WriteAllText(
        $SecretFile,
        $ApiKey,
        $Utf8NoBom
    )

    # literal: означает точное совпадение, не regex
    [IO.File]::WriteAllText(
        $ReplaceFile,
        "literal:$ApiKey==>***REMOVED***",
        $Utf8NoBom
    )

    # --------------------------------------------------------
    # Fresh mirror clone
    # --------------------------------------------------------

    Write-Host ""
    Write-Host "Создаю временный mirror clone..."

    git clone --mirror $OriginUrl $RepoDir

    if ($LASTEXITCODE -ne 0) {
        throw "git clone завершился с ошибкой."
    }

    Set-Location $RepoDir

    # --------------------------------------------------------
    # Callback: удалить ТОЛЬКО эти два коммита
    # --------------------------------------------------------

    $Callback = @'
if commit.original_id in {
    b'2b1e6ae128c88c28cb17d9f0732a5eb16b6d2afa',
    b'3fe655b1575390fc4b8ca6fe3769af614858f5ea',
}:
    commit.skip()
'@

    Write-Host ""
    Write-Host "Переписываю историю..."

    git filter-repo `
        --force `
        --sensitive-data-removal `
        --replace-text $ReplaceFile `
        --replace-message $ReplaceFile `
        --prune-empty never `
        --prune-degenerate never `
        --commit-callback $Callback

    if ($LASTEXITCODE -ne 0) {
        throw "git filter-repo завершился с ошибкой."
    }


# --------------------------------------------------------
# Проверяем, что плохие коммиты больше не reachable
# --------------------------------------------------------

Write-Host ""
Write-Host "Checking removed commits..."

$AllReachableCommits = @(git rev-list --all)

foreach ($BadCommit in @($BadCommit1, $BadCommit2)) {

    if ($AllReachableCommits -contains $BadCommit) {
        throw "ERROR: commit is still reachable: $BadCommit"
    }

    Write-Host "OK: commit is no longer reachable: $BadCommit"
}

    # --------------------------------------------------------
# Remove old unreachable objects
# --------------------------------------------------------

Write-Host ""
Write-Host "Removing old Git objects..."

git reflog expire --expire=now --all
git gc --prune=now

$PreviousErrorActionPreference = $ErrorActionPreference

try {
    # git cat-file is EXPECTED to return an error when the object is gone.
    # Windows PowerShell 5.1 otherwise turns stderr into a terminating error.
    $ErrorActionPreference = "Continue"

    foreach ($BadCommit in @($BadCommit1, $BadCommit2)) {

        & git cat-file -e "$BadCommit^{commit}" 2>$null
        $ObjectExists = ($LASTEXITCODE -eq 0)

        if ($ObjectExists) {
            throw "ERROR: old commit object still exists: $BadCommit"
        }

        Write-Host "OK: old object does not exist: $BadCommit"
    }
}
finally {
    $ErrorActionPreference = $PreviousErrorActionPreference
}

    # --------------------------------------------------------
    # Проверяем ВСЕ reachable commits на API key
    # --------------------------------------------------------

    Write-Host ""
    Write-Host "Проверяю все коммиты на наличие API key..."

    $Commits = git rev-list --all

    foreach ($Commit in $Commits) {

        git grep `
            -F `
            -q `
            -f $SecretFile `
            $Commit `
            -- `
            2>$null

        if ($LASTEXITCODE -eq 0) {
            throw "API key всё ещё найден в commit: $Commit"
        }
    }

    Write-Host "OK: API key не найден ни в одном reachable commit."

    # --------------------------------------------------------
    # Добавляем origin обратно
    # git-filter-repo обычно специально его удаляет
    # --------------------------------------------------------

$Remotes = @(git remote)

if ($Remotes -contains "origin") {
    git remote set-url origin $OriginUrl

    if ($LASTEXITCODE -ne 0) {
        throw "Failed to update origin."
    }
}
else {
    git remote add origin $OriginUrl

    if ($LASTEXITCODE -ne 0) {
        throw "Failed to add origin."
    }
}

    # --------------------------------------------------------
    # PUSH
    # --------------------------------------------------------

    Write-Host ""
    Write-Host "Переписываю remote..."
    Write-Host ""

    git push --force --mirror origin

    $PushResult = $LASTEXITCODE

    if ($PushResult -ne 0) {
        Write-Host ""
        Write-Warning "Push завершился с ошибками."
        Write-Warning "Проверь вывод выше."
        Write-Warning ""
        Write-Warning "На GitHub ошибки для refs/pull/* могут быть ожидаемы,"
        Write-Warning "поскольку GitHub не разрешает force-push этих refs."
    }
    else {
        Write-Host ""
        Write-Host "==============================================="
        Write-Host "ГОТОВО"
        Write-Host "==============================================="
        Write-Host ""
        Write-Host "Удалены:"
        Write-Host "  $BadCommit1"
        Write-Host "  $BadCommit2"
        Write-Host ""
        Write-Host "API key удалён из reachable Git history."
    }

}
finally {

    # Стараемся убрать ключ из переменной
    $ApiKey = $null
    $SecureKey = $null

    Set-Location $PSScriptRoot

    if (Test-Path $WorkDir) {
        Remove-Item `
            -Recurse `
            -Force `
            $WorkDir `
            -ErrorAction SilentlyContinue
    }
}