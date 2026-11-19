# iOS 단축어 (소스 → 서명 파일)

`의료비 저장.shortcut`, `Pick3 저장.shortcut`은 서명된 설치 파일입니다. iPhone Safari에서 열면 단축어 앱으로 가져옵니다.
토큰은 들어 있지 않습니다. 설치 후 시작 페이지의 **[단축어에 내 계정 연결]**이 토큰을 `Shortcuts/pick3_token.txt`에 저장합니다.

다시 만들기 (Windows + WSL Ubuntu, [Cherri](https://github.com/electrikmilk/cherri) v2.3.0 Linux 바이너리 `~/cherri/cherri`):

```
~/cherri/cherri medical.cherri --skip-sign      # -> medical_unsigned.shortcut
~/cherri/cherri pick3.cherri --skip-sign
python patch.py medical pick3                    # form 필드 image 를 '파일' 형식으로 (Cherri는 텍스트만 만듦)
python sign.py                                   # RoutineHub HubSign 서비스로 서명 -> *.shortcut
```

서명은 Mac(`shortcuts sign`)이 없어서 HubSign 외부 서비스를 씁니다. 보내는 내용은 단축어 동작과 서버 주소뿐입니다.
