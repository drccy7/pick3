# iOS 단축어 (소스 → 서명 파일)

`Pick3.shortcut`(화면 캡처·PDF·파일 모두, 서버가 맛집·의료비·처방을 구분)은 서명된 설치 파일입니다. iPhone Safari에서 열면 단축어 앱으로 가져옵니다.
토큰은 들어 있지 않습니다. 설치 후 시작 페이지의 **[단축어에 내 계정 연결]**이 토큰을 `Shortcuts/pick3_token.txt`에 저장합니다.

다시 만들기 (Windows + WSL Ubuntu, [Cherri](https://github.com/electrikmilk/cherri) v2.3.0 Linux 바이너리 `~/cherri/cherri`):

```
~/cherri/cherri pick3.cherri --skip-sign
python patch.py pick3                    # form 필드 image 를 '파일' 형식으로 (Cherri는 텍스트만 만듦)
python sign.py                                   # RoutineHub HubSign 서비스로 서명 -> *.shortcut
```

서명은 Mac(`shortcuts sign`)이 없어서 HubSign 외부 서비스를 씁니다. 보내는 내용은 단축어 동작과 서버 주소뿐입니다.

단축어 이름은 영문만 씁니다. 파일로 가져온 단축어는 한글 이름이 자모 분리(NFD)로 저장되어 `shortcuts://run-shortcut?name=` 으로 찾지 못하기 때문입니다.

단축어는 받아서 보내고 결과를 보여 주는 일만 합니다. 판단은 모두 서버가 하므로 기능을 바꿀 때 단축어(와 iCloud 링크)는 그대로 둡니다.
