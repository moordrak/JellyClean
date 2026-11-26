# JellyClean

Web servis za pregled i čišćenje prostora na Jellyfin poslužitelju. Nema vanjskih ovisnosti, treba samo Python 3.9 ili noviji.

## Što radi

- **Pregled**: slobodan i zauzet prostor po disku, veličina svake medijske mape i njezinih najvećih podmapa, podjela po vrsti sadržaja (video, titlovi, slike, nfo) i po ekstenziji.
- **Preglednik**: prolaz kroz mape sortirane po veličini (kao `ncdu`), s mogućnošću označavanja i brisanja.
- **Najveće datoteke**: filtri po minimalnoj veličini, starosti, ekstenziji, dijelu putanje i mapi.
- **Jellyfin** (preko API-ja):
  - filmovi i serije s podatkom tko ih je pogledao, kad su zadnji put gledani i kad su dodani
  - filtri *pogledali svi korisnici*, *nitko nije pogledao* i *nije gledano X dana*
  - brisanje cijelog filma (mapa), cijele serije ili samo epizoda koje su pogledali svi
  - video datoteke koje postoje na disku, a Jellyfin ih ne poznaje
  - pokretanje Jellyfinovih zadataka održavanja (čišćenje cache i transcode mapa, logova itd.)
- **Jellyfin config** (preko SSH-a do računala na kojem radi Jellyfin):
  - zauzeće mapa `cache`, `cache/images`, `cache/transcodes`, `log`, `data/metadata` i baze
  - čišćenje slika u cacheu, ostataka transkodiranja (starijih od 1 sata), ostatka cachea i logova starijih od X dana, uvijek s pregledom prije brisanja
  - **siročad u `metadata/library`**: mape filmova i epizoda koje više ne postoje. Uspoređuju se s Jellyfin API-jem, a svaka se još pojedinačno provjerava prije brisanja
  - baza (`*.db`) se nikad ne dira
- **Provjera putanja**: uspoređuje putanje u Jellyfin bazi sa stvarnim datotekama.
  - Ako na zapisanoj putanji nema datoteke, traži je drugdje: isto ime, druga velika i mala slova, drugačiji Unicode zapis znakova (č/ć/ž), ista veličina, ista oznaka SxxEyy u mapi serije.
  - Prepoznaje i epizode koje Jellyfin ima bez datoteke, iako ona postoji (poruka *Unable to find a valid media source to play*).
  - Kad se ista promjena putanje ponavlja kod više stavki, javlja da je vjerojatno promijenjen mount ili putanja biblioteke.
  - **Siguran ispravak:** Jellyfinu se preko API-ja javi promjena (`/Library/Media/Updated`) i osvježi se serija.
  - **Ispravak u bazi** (uz SSH pristup): zaustavi Jellyfin, napravi kopiju `jellyfin.db`, promijeni samo putanje odabranih zapisa (ID i gledanost ostaju) i ponovno ga pokrene. Ako struktura baze nije prepoznata, ništa se ne mijenja.
- **Zašto se ne pušta?** Dijagnoza reprodukcije za odabrani film ili epizodu, preko pretrage ili gumba *Zašto?* u Provjeri putanja:
  - zapis u bazi, izvori (MediaSources) i `PlaybackInfo` iz Jellyfina
  - datoteka na NAS-u i traženje datoteke drugdje
  - preko SSH-a: vidi li datoteku i može li je čitati **sam Jellyfin kontejner**, `ffprobe` provjera sadržaja te greške za tu stavku iz Jellyfin i FFmpeg logova
  - jasan zaključak (zapis bez datoteke, kriva putanja, mount, dozvole, oštećena datoteka, neuspjelo transkodiranje, problem do klijenta) i popravak jednim klikom (osvježi stavku ili javi Jellyfinu datoteku)
- **Test paralelnog enkodiranja**: koliko istovremenih transkodiranja NVIDIA kartica u Jellyfinu može odraditi.
- **Nadzor kartice uživo** (na kartici Test enkodiranja): GPU, ENC, DEC, VRAM, temperatura, snaga, ventilator i takt u stvarnom vremenu, graf zadnjih 5 minuta i popis procesa na kartici (koja se datoteka transkodira, u što, koliko ENC/DEC/VRAM troši). Isti NVML podaci koje prikazuje nvtop.
- **NVENC patch drivera**: uklanja ograničenje broja istovremenih NVENC sesija na GeForce karticama ([keylase/nvidia-patch](https://github.com/keylase/nvidia-patch)), uz sigurnosnu kopiju, vraćanje originala, provjeru s 13 istovremenih sesija i upozorenje kad nadogradnja drivera poništi patch.
  - testnu datoteku biraš iz biblioteke, a test se preko SSH-a izvodi **u Jellyfin kontejneru** s Jellyfinovim ffmpegom (NVDEC → `scale_cuda` → `h264_nvenc`)
  - broj istovremenih tokova raste korak po korak, uz praćenje brzine svakog toka te opterećenja ENC, DEC, GPU i VRAM
  - dva načina: *maksimalna brzina* (granica kartice i drivera) i *stvarno vrijeme* (opterećenje kao kod gledanja)
  - rezultat: koliko tokova kartica stiže, preporučeni broj s rezervom i što ju je ograničilo (ograničenje sesija drivera ili snaga)
  - upozorava ako netko upravo gleda s transkodiranjem; test se može prekinuti
- **Smeće i duplikati**: sample videi, nedovršena preuzimanja (`.part`, `.!qB`), `.exe` i `.url` datoteke iz releasea, rar arhive, prazne datoteke i mape, više verzija istog filma ili epizode.
- **Kanta**: obrisane stavke se premještaju u `.jellyclean-trash` unutar iste mape, što je trenutno jer se ništa ne kopira. Iz kante se mogu vratiti, a automatski se prazni nakon `TRASH_DAYS` dana.
- **Hardlinkovi**: servis označava datoteke koje imaju još jednu kopiju (tipično *arr + qBittorrent) i upozorava da njihovo brisanje ne oslobađa prostor dok se ne obriše i torrent.
- Nakon brisanja Jellyfin sam pokreće osvježavanje biblioteke.

## Konfiguracija (varijable okruženja)

| Varijabla | Primjer | Opis |
|---|---|---|
| `MEDIA_PATHS` | `Filmovi=/media/movies,Serije=/media/tv` | Medijske mape koje se skeniraju i iz kojih se smije brisati |
| `JELLYFIN_DIRS` | `Config=/jellyfin/config,Cache=/jellyfin/cache` | Jellyfin mape koje se samo prikazuju, bez brisanja |
| `JELLYFIN_URL` | `http://192.168.1.10:8096` | Adresa Jellyfina |
| `JELLYFIN_API_KEY` | | Nadzorna ploča → API ključevi → + |
| `PATH_MAP` | `/data/movies=/media/movies;/data/tv=/media/tv` | Kad Jellyfin vidi medije pod drugom putanjom nego JellyClean, format je `jellyfin=jellyclean` |
| `JELLYFIN_USERS` | `ivan,ana` | Koji se korisnici broje za *pogledali svi*. Prazno znači svi aktivni korisnici |
| `ALLOW_DELETE` | `true` | `false` uključuje način samo za pregled |
| `TRASH_MODE` | `true` | `false` znači da je brisanje odmah trajno |
| `TRASH_DAYS` | `7` | Nakon koliko dana se kanta automatski prazni (`0` znači nikad) |
| `SCAN_INTERVAL_HOURS` | `6` | Koliko često se automatski ponovno skenira |
| `AUTH_USER` / `AUTH_PASS` | | Basic auth. **Postavi ga**, jer sučelje može brisati datoteke |
| `PORT` | `8095` | |
| `JF_SSH_HOST` | `192.168.1.20` | Računalo na kojem radi Jellyfin (npr. VM s Dockerom), za čišćenje config mape |
| `JF_SSH_USER` / `JF_SSH_PORT` | `root` / `22` | SSH korisnik i port |
| `JF_SSH_KEY` | `/var/lib/jellyclean/ssh/id_ed25519` | Privatni ključ (zadano `DATA_DIR/ssh/id_ed25519`) |
| `JF_SSH_SUDO` | `false` | `true` ako SSH korisnik ima **potpuni** sudo bez lozinke (ne preporučuje se) |
| `JF_SSH_HELPER` | `/usr/local/sbin/jellyclean-helper` | Ograničeni pomoćni program na Jellyfin računalu, pokreće se sa `sudo -n` (postavlja ga LXC skripta) |
| `JF_CONFIG_PATH` | `/opt/jellyfin/config` | Jellyfin `/config` mapa na tom računalu |
| `JF_CACHE_PATH`, `JF_LOG_PATH`, `JF_DATA_PATH`, `JF_METADATA_PATH` | | Ako se razlikuju od zadanih (`config/cache`, `config/log`, `config/data`, `data/metadata`). Kod službenog Docker imagea su cache i data drugdje; LXC skripta to otkriva sama |
| `JELLYFIN_VERIFY_TLS` | `true` | `false` za samopotpisani certifikat (npr. `.int` domena) |

## Pokretanje

### Docker

Uredi `docker-compose.yml` (volumene, URL i ključ), zatim pokreni:

```bash
docker compose up -d --build
```

`user:` mora biti isti UID i GID koji imaju medijske datoteke, inače brisanje javlja *Permission denied*.

### Proxmox: novi LXC jednom naredbom (preporučeno)

Na Proxmox **hostu** kao root:

```bash
# javni repozitorij
bash -c "$(curl -fsSL https://raw.githubusercontent.com/moordrak/JellyClean/main/proxmox/create-lxc.sh)"

# privatni repozitorij: kloniraj ga na host pa pokreni lokalno
git clone https://github.com/moordrak/JellyClean.git && bash JellyClean/proxmox/create-lxc.sh
```

Instalacija radi u **whiptail dijaloškim prozorima**, kao Proxmox helper skripte. Na vrhu ekrana stoji *Korak x/7*, a na početku ASCII logo. Dulji poslovi (kreiranje kontejnera, paketi, otkrivanje) ispisuju napredak u konzoli. *Odustani* uvijek pita prekidaš li instalaciju ili nastavljaš.
Bez prozora, sve u konzoli: `NO_WHIPTAIL=1 bash create-lxc.sh`.

Skripta kreira **privileged** LXC i NFS shareove s NAS-a montira **unutar kontejnera** (`/etc/fstab` u kontejneru). Na Proxmox host se ne dodaje ništa.

Većinu postavki otkriva sama, a prije svake promjene pojasni prijedlog i traži odobrenje:

| Korak | Što se otkriva | Kako |
|---|---|---|
| Kontejner | ID, storage s najviše mjesta, bridge, resursi | predlaže se sve odjednom; možeš prihvatiti ili mijenjati |
| NFS | exporti na NAS-u | `pvesm scan nfs` ili `showmount -e` (samo čitanje). Exporti s imenom koje upućuje na medije unaprijed su označeni. Ako NAS nudi samo NFSv4, upisuju se ručno |
| Biblioteke | Jellyfin biblioteke i njihove putanje | Jellyfin API (`/Library/VirtualFolders`) |
| Povezivanje | koja mapa na NAS-u odgovara kojoj biblioteci | nekoliko stvarnih datoteka iz Jellyfina traži se na montiranim shareovima (npr. „✓ 8/8 datoteka”). Radi i kad se mape zovu različito (`/data/tvshows` ↔ `/mnt/nas/media/tv`) |
| `PATH_MAP` | Jellyfin putanja → putanja u kontejneru | iz povezivanja |
| Korisnik | UID i GID vlasnika medija | iz vlasnika pronađenih datoteka. Upozorava na `nobody` (squash) |
| Prava | može li servis brisati | test pisanja kao taj korisnik u svakoj mapi |

Bez Jellyfina skripta predlaže mape prema imenu (`movies`, `filmovi`, `tv`, `serije`, `anime`…). Biblioteka koja nije pronađena može se ručno povezati s mapom.

**Jellyfin config preko SSH-a** (opcionalno):
- u kontejneru se napravi SSH ključ samo za JellyClean
- ključ se postavi na Jellyfin računalo: upišeš lozinku jednom (`ssh-copy-id`) ili ga dodaš ručno
- putanje se otkrivaju same, preko `docker inspect` za kontejner s „jellyfin” u imenu (linuxserver ili službeni image) ili kao standardna paketna instalacija (`/var/lib/jellyfin`)
- **SSH korisnik ne mora biti root.** Skripta prvo provjeri ima li korisnik već prava na cache, log i metadata mape, što je čest slučaj kod linuxserver imagea s `PUID` jednakim UID-u korisnika. Ako ima, ništa se ne mijenja.
- Ako nema, a root se dobiva preko `sudo` uz lozinku, skripta lozinku pita **jednom** i ne sprema je. Koristi je za otkrivanje putanja i za postavljanje **ograničenog pomoćnog programa**:
  - `/usr/local/sbin/jellyclean-helper`, vlasnik root. Zna samo izmjeriti mape te obrisati cache, stare logove i siročad u metadata, i to samo u putanjama upisanima u njega. Uz to pokreće test enkodiranja i NVENC patch: patch skripta stiže preko SSH-a, ali se pokreće samo ako se njen SHA256 nalazi na popisu odobrenih verzija upisanom u pomoćni program
  - `/etc/sudoers.d/jellyclean`, provjeren s `visudo`: `<korisnik> ALL=(root) NOPASSWD: /usr/local/sbin/jellyclean-helper`

  Tako JellyClean i njegov SSH ključ nemaju potpuni root na tom računalu. Uklanjanje: `sudo rm /usr/local/sbin/jellyclean-helper /etc/sudoers.d/jellyclean`

Kad nema ograničenja ovlasti, na Jellyfin računalu se ništa ne instalira.

Ručno se upisuju samo IP adresa NAS-a, Jellyfin URL i API ključ te, za čišćenje configa, adresa Jellyfin računala. Sve ostalo ima prijedlog.

Ako se NFS export ne montira, najčešći uzrok je da NAS ne dopušta IP adresu kontejnera. Dodaj je u NFS dozvole na NAS-u.

Upravljanje kontejnerom:

```bash
pct exec <ID> -- nano /etc/jellyclean.env      # konfiguracija, pa: systemctl restart jellyclean
pct exec <ID> -- journalctl -u jellyclean -f   # logovi
pct exec <ID> -- /usr/local/bin/jellyclean-update   # nadogradnja (privatni repo: dodaj env GH_TOKEN=xxx ispred)
```

### Nadogradnja postojećeg kontejnera (s Proxmox hosta)

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/moordrak/JellyClean/main/proxmox/update-lxc.sh)"
```

Skripta sama pronađe JellyClean kontejnere (po tagu ili hostnameu) ili ih prima kao argumente (`update-lxc.sh 105`). U svaki postavi ili osvježi `/usr/local/bin/jellyclean-update` i pokrene ga. Nadogradnja radi i za instalaciju iz gita (`git pull`) i bez gita (preuzme se najnoviji release). Za privatni repozitorij: `GH_TOKEN=xxx bash update-lxc.sh`.

### Postojeći LXC

```bash
# unutar LXC-a, s već montiranim medijima
bash proxmox/install-in-lxc.sh
nano /etc/jellyclean.env && systemctl restart jellyclean
```

### Ručno

```bash
MEDIA_PATHS="Filmovi=/mnt/media/movies" JELLYFIN_URL=http://jf:8096 JELLYFIN_API_KEY=xxx python3 app.py
```

## Napomene

- **Radarr/Sonarr**: ako je film ili serija praćen (*monitored*), *arr će ga nakon brisanja ponovno preuzeti. Prije brisanja ga isključi iz praćenja ili ga obriši kroz sam Radarr/Sonarr.
- Skener čita samo metapodatke datoteka i ne otvara sam video. Na velikim NFS mapama prvo skeniranje ipak može potrajati nekoliko minuta.
- Kanta je na istom disku, pa se prostor stvarno oslobađa tek kad se kanta isprazni.
- POST zahtjevi traže header `X-Requested-With: JellyClean` kao zaštitu od CSRF-a.

## NVENC patch drivera

Kartica *Test enkodiranja* → **Ograničenje NVENC sesija**:

- pokazuje verziju drivera na Jellyfin računalu, je li driver patchan i postoji li patch za tu verziju
- **Primijeni patch**: zaustavi Jellyfin, spremi original biblioteke (`/opt/nvidia/libnvidia-encode-backup`), primijeni patch, ponovno pokrene Jellyfin i provjeri 13 istovremenih sesija
- **Vrati original**: vraća sigurnosnu kopiju
- **Provjeri ograničenje**: 13 kratkih istovremenih NVENC enkodiranja unutar Jellyfin okruženja
- nakon nadogradnje drivera javlja da je patch nestao

Koristi se zakucana, provjerena verzija skripte `patch.sh` iz keylase/nvidia-patch (commit u `NVPATCH_COMMIT`). JellyClean je preuzima s GitHuba i provjerava SHA256, a pomoćni program na Jellyfin računalu provjerava ga ponovno prije pokretanja. Ako LXC nema pristup GitHubu, datoteku možeš ručno spremiti u `DATA_DIR/nvidia-patch.sh`.

Patch se primjenjuje na računalu na kojem je instaliran NVIDIA driver (VM ili host s Dockerom). Ne radi kad je Jellyfin u LXC-u kojem driver dolazi s Proxmox hosta. Patch je neslužben i protivan NVIDIA licenci za GeForce kartice. Traži `ALLOW_DELETE=true`.

## Više Jellyfin servera

JellyClean može upravljati s više Jellyfin servera koji koriste **iste medije** (isti NAS):

- **Glavni server** dolazi iz `/etc/jellyclean.env`, kako ga postavi instalacijska skripta.
- **Ostale servere** dodaješ u sučelju, na kartici **Serveri → + Dodaj server**: naziv, URL, API ključ, PATH_MAP te SSH host i korisnik.
- **Postavi pristup** daje jednu naredbu (`curl … | sudo sh`) koju pokreneš na računalu s Jellyfinom. Naredba dopusti JellyCleanovom SSH ključu prijavu, pronađe Jellyfin (Docker ili paket) i postavi pomoćni program sa sudo pravilom samo za njega. Poveznica vrijedi 30 minuta, a sudo lozinka se nigdje ne sprema. Ista naredba služi i za nadogradnju pomoćnog programa na tom računalu.
- **Izbornik u zaglavlju** bira server za kartice Jellyfin, Provjera putanja, Jellyfin config i Test enkodiranja (uključujući nadzor kartice i NVENC patch).
- Kartice Pregled, Preglednik, Najveće datoteke, Smeće i duplikati i Kanta su zajedničke, jer su mediji isti.
- Nakon brisanja ili vraćanja datoteka JellyClean javlja promjenu **svim** serverima. Automatska sinkronizacija također obuhvaća sve servere.

Postavke servera spremaju se u `DATA_DIR/servers.json` (prava 600). Stanje po serveru, kao rezultati testa enkodiranja i NVENC patcha, sprema se u `DATA_DIR/servers/<id>/`. Izmjene glavnog servera napravljene u sučelju imaju prednost pred env datotekom, a gumb **Vrati iz env** ih poništava.

