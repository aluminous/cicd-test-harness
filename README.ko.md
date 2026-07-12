# CI/CD 테스트 하네스

[English](https://github.com/aluminous/cicd-test-harness/blob/main/README.md) |
[한국어](https://github.com/aluminous/cicd-test-harness/blob/main/README.ko.md)

> **알파 프리뷰:** 검증된 워크플로는 정상 동작하지만, 공개 API와 프로필 구조는
> `1.0` 이전에 변경될 수 있습니다. 제공되는 인프라는 의도적으로 일회성이며,
> 프로덕션이나 공유 클러스터에 사용하기에는 적합하지 않습니다.

이 저장소는 Kind, Argo Rollouts, Istio, Jenkins, Gitea, WireMock 및 최소한의
Spinnaker 서비스 구성으로 일회성 CI/CD 테스트 환경을 제공합니다.

이 하네스는 각 구성 요소의 네이티브 인터페이스를 사용하면서 Testcontainers와
유사한 생명주기를 따릅니다.

- `kind`가 Kubernetes 클러스터를 관리합니다.
- `kubectl`과 Helm이 클러스터 내부 리소스를 관리합니다.
- Python이 의존성 순서, 준비 상태, 진단 및 종료를 관리합니다.
- pytest 테스트는 셸 출력 대신 타입이 지정된 fixture를 사용합니다.

## 요구 사항

- Python 3.11 이상
- Docker 또는 Podman과 전체 스택에 사용할 수 있는 최소 8 GiB 메모리
- `PATH`에 등록된 `kubectl`, Helm 및 Git
- Kubernetes 1.21 레거시 프로필을 위한 rootful Podman 또는 Docker/DinD

프로필별 Kind 바이너리는 처음 사용할 때 `.tools/bin`에 다운로드되며 플랫폼별
SHA-256 체크섬으로 검증됩니다. Kubernetes 노드 이미지와 구성 요소 이미지는
선택한 프로필의 digest 또는 버전으로 고정됩니다.

## 설치

소스 개발 환경:

```bash
git clone https://github.com/aluminous/cicd-test-harness.git
cd cicd-test-harness
uv sync --extra dev
uv run pytest
```

빌드된 wheel은 자체 완결적이며 기본 프로필, 매니페스트, Helm chart, 이미지 빌드
레시피 및 저장소 fixture를 포함합니다. 프리뷰가 배포된 후에는 일반 개발 의존성으로
설치할 수 있습니다.

```bash
uv add --dev cicd-test-harness
uv run cicd-harness profile show modern
```

프로젝트의 `profiles/<name>.yaml`은 같은 이름의 내장 프로필보다 우선합니다. 따라서
설치된 패키지를 수정하지 않고도 사설 레지스트리나 대체 구성 요소 이미지를 고정할
수 있습니다.

## 내장 프로필

| 프로필 | 런타임 | Kubernetes | Argo Rollouts | Istio | PoC 상태 |
|---|---|---:|---:|---:|---|
| `modern` | Podman 또는 Docker | 1.31.14 | 1.8.3 | 1.25.5 | arm64 Podman에서 검증됨 |
| `legacy` | Rootful Podman 또는 Docker/DinD | 1.21.14 | 1.4.1 | 1.10.6 | 문서화된 Istio shim을 사용해 arm64 rootful Podman에서 검증됨 |

모든 이미지와 도구 참조는 고정되어야 합니다. 런타임에 다운로드되는 릴리스 자산은
의도적으로 커밋하지 않는 `.tools/` 아래에 캐시됩니다.

## 빠른 시작

```bash
uv run cicd-harness profile show modern
uv run cicd-harness doctor modern --prepare
uv run cicd-harness stack-up modern
uv run cicd-harness endpoints modern
uv run cicd-harness expose modern gitea jenkins
uv run cicd-harness stack-down modern
uv run pytest
```

`stack-up`은 Kind 클러스터를 생성하고 Rollouts, Istio와 단일 ingress gateway,
Gitea, WireMock, Jenkins 및 최소 Spinnaker 구성을 설치하며 UI 조작 없이 Gitea를
초기화합니다. 더 작은 테스트에는 `--without-spinnaker` 또는 `--without-jenkins`를
사용할 수 있습니다. 정확한 구성 요소 그래프를 선택하려면
`--components argo-rollouts,istio,wiremock`을 사용합니다.

`endpoints`는 아무것도 시작하지 않고 호스트에서 접근 가능한 UI와 API를 나열합니다.
`expose`는 기존 스택에 loopback 전용 `kubectl port-forward` 프로세스를 연결하고
Ctrl-C를 누를 때까지 foreground에서 유지됩니다. 이 방식은 Podman VM 포트 매핑 없이
rootful Podman을 통과하며 Istio에 의존하지 않습니다.

`doctor`는 선택한 컨테이너 런타임 연결, `kubectl`, Helm, Git 및 프로필 메모리
예산을 확인합니다. `--prepare`를 사용하면 클러스터를 생성하지 않고 고정된 Kind
바이너리도 다운로드하고 체크섬을 검증합니다.

인프라 PoC는 컨테이너를 생성하므로 명시적으로 활성화해야 합니다.

```bash
CICD_RUN_POC=1 uv run pytest -m poc -s
```

애플리케이션 테스트에서는 클러스터와 클라이언트를 직접 조립하는 대신 `harness`
pytest fixture를 사용해야 합니다. 이 fixture는 고수준 Git, outbound mock, Jenkins,
Spinnaker 및 Rollout 작업을 제공하고, 격리된 namespace를 소유하며, mock을 검증하고,
실패 시 클러스터와 구성 요소 진단을 자동 수집합니다. 테스트 작성자용 API와 저수준
escape hatch는
[`docs/testing-api.md`](https://github.com/aluminous/cicd-test-harness/blob/main/docs/testing-api.md)를
참조하십시오.

WireMock 기반 reverse proxy는 실제 서비스로 요청을 전달하고 정규화된 요청 증거를
기록하며, 하네스가 Istio에 결합되지 않은 상태에서 선택한 응답을 대체할 수 있습니다.
쓰기 가능한 저장소는 이름이 지정된 fixture 디렉터리에서 재귀적으로 초기화할 수
있습니다. `harness.jenkins.create_library()`는 고유한 라이브러리 저장소를 만들고
동적으로 등록하므로 공유 라이브러리를 추가하기 위해 매니페스트나 UI를 변경할 필요가
없습니다.

매니페스트를 수정하지 않고 모든 구성 이미지가 사설 레지스트리를 사용하도록
리디렉션할 수 있습니다. 레지스트리 자격 증명은 환경 변수 이름으로 지정되고 비공개
임시 Docker/Podman 인증 파일로 생성되며 Kubernetes pull secret으로 설치된 후 종료
시 제거됩니다. `harness.resources`로 적용한 애플리케이션 매니페스트도 동일한 rewrite와
pull secret 동작을 상속합니다. 설정 방법은
[`docs/testing-api.md`](https://github.com/aluminous/cicd-test-harness/blob/main/docs/testing-api.md#private-registries)를
참조하십시오.

특정 버전 프로필은 `CICD_PROFILE=modern` 또는 `CICD_PROFILE=legacy`로 명시적으로
선택할 수 있습니다. Kubernetes 1.21은 현재 rootless Podman VM에서 시작할 수 없으므로
레거시 프로필에는 rootful Podman 연결 또는 Docker/DinD가 필요합니다. Apple Silicon에서는
정확한 소스로부터 arm64 Istio 1.10.6 pilot shim을 자동으로 빌드합니다. 재현 가능한
Docker/Podman 빌드 명령, GHCR 게시 방법 및 gateway fidelity 경계는
[`ARM64 호환성 가이드`](https://github.com/aluminous/cicd-test-harness/blob/main/docs/arm64-compatibility.md)를
참조하십시오.

macOS의 현재 PoC는 기존 Podman machine을 지원합니다. CI에서는 모든 하위 프로세스가
하나의 8 GiB cgroup을 공유하도록 privileged DinD 하네스 컨테이너를 사용할 예정입니다.

## Mock API 예시

```python
scanner = mocks.service("scanner")
expectation = scanner.expect(
    method="POST",
    path="/v1/scans",
    response={"status": 202, "json": {"scanId": "scan-123"}},
    json_paths={"$.repository": "payments"},
    times=1,
)

# 백엔드 또는 Jenkins job 실행

expectation.verify()
```

WireMock은 결정적인 HTTP 응답을 반환하고 메모리 내 요청 journal을 유지합니다. 하네스
wrapper는 테스트 사이에 mapping을 초기화하고 호출 횟수를 검증하며, 일치하지 않는
outbound 호출을 보고하고, 각 expectation을 작은 Python 객체로 제공합니다.

## 검증된 워크플로

- Gitea 저장소 생성, 인증된 commit/push 및 정확한 commit 기준 raw fetch
- WireMock host/path/header/JSONPath matching, 결정적 응답, 정확한 호출 횟수 검증 및
  일치하지 않는 요청 보고
- Kubernetes 1.31/1.21과 Istio 1.25/1.10에서 Argo Rollouts canary, 50/50
  VirtualService weight, stable/canary ReplicaSet 검사 및 지연된 이전 ReplicaSet
  scale-down 증거 확인
- Spinnaker 1.25.4 raw manifest pipeline: Gate -> Orca -> Clouddriver -> 정확한 Gitea
  commit -> Kubernetes
- Spinnaker Kustomize pipeline: Gate -> Orca -> Rosco -> embedded artifact ->
  Clouddriver -> Kubernetes
- Jenkins 2.426.1 REST trigger -> shell job -> 인증된 Gitea push -> 검증된 WireMock callback
- Jenkins multibranch job 생성 및 설정 검사 -> Gitea branch discovery -> 저장소의
  `Jenkinsfile` -> 외부 `@Library('example')` checkout 및 실행
- 관련 없는 controller를 제외한 Jenkins/Gitea/WireMock callback 흐름과 fixture 소유
  namespace에 배포하는 Rollouts/Istio/Gitea/Spinnaker 흐름의 정확한 구성 요소 subset
- rootful Podman을 통한 호스트 endpoint 검색과 loopback 노출, 실패 후 보존된 테스트
  namespace 재연결 및 CLI를 통한 명시적 삭제

최소 Spinnaker 런타임은 Gate, Orca, Clouddriver, Front50, Rosco, Redis 및 MinIO로
구성됩니다. Deck, Echo, Igor, Fiat, Kayenta 및 Halyard는 제외됩니다. Pipeline plugin을
추가하기 전 전체 modern 노드는 Spinnaker canary 동안 5.6-5.8 GiB를 사용했고 Jenkins까지
실행했을 때 6.01 GiB를 사용했습니다. Pipeline Jenkins, 7개 Spinnaker 서비스 및 완료된
multibranch/shared-library 빌드를 함께 실행했을 때 서비스 준비 중 5.84 GB, 빌드 후
5.68 GB를 사용했으며 pod restart는 없었습니다. 전체 legacy 노드는 raw 및 Kustomize
pipeline 실행 후 6.05 GiB를 사용했습니다.

Spinnaker는 Argo Rollout 상태와 별개로 deploy stage 성공을 보고하므로 테스트는 pipeline
실행과 `RolloutProbe.wait_healthy()`를 모두 검증해야 합니다. PoC에서 발견한 호환성 함정,
메모리 튜닝 이력 및 리팩터링 지침은
[`docs/engineering-notes.md`](https://github.com/aluminous/cicd-test-harness/blob/main/docs/engineering-notes.md)를
참조하십시오.

대표 애플리케이션 테스트 커버리지와 알려진 v1 DX 경계는
[`docs/dx-coverage.md`](https://github.com/aluminous/cicd-test-harness/blob/main/docs/dx-coverage.md)에
정리되어 있습니다. PoC 이후 구조, 완료된 정리 및 다음 리팩터링 기준은
[`docs/architecture-review.md`](https://github.com/aluminous/cicd-test-harness/blob/main/docs/architecture-review.md)에
있습니다. 의도적으로 작은 구성 요소 확장 계약은
[`docs/components.md`](https://github.com/aluminous/cicd-test-harness/blob/main/docs/components.md)에
문서화되어 있습니다. ARM64 네이티브, 에뮬레이션 및 호환성 이미지 경계는
[`docs/arm64-compatibility.md`](https://github.com/aluminous/cicd-test-harness/blob/main/docs/arm64-compatibility.md)에
요약되어 있습니다.

## 기여 및 라이선스

기여를 환영합니다. 변경 사항이나 보안 보고서를 제출하기 전에
[`CONTRIBUTING.md`](https://github.com/aluminous/cicd-test-harness/blob/main/CONTRIBUTING.md)와
[`SECURITY.md`](https://github.com/aluminous/cicd-test-harness/blob/main/SECURITY.md)를
확인하십시오. 이 하네스는
[`MIT License`](https://github.com/aluminous/cicd-test-harness/blob/main/LICENSE)로
제공됩니다. 포함된 upstream 자료는
[`THIRD_PARTY_NOTICES.md`](https://github.com/aluminous/cicd-test-harness/blob/main/THIRD_PARTY_NOTICES.md)에
문서화되어 있습니다. 유지관리자는 artifact를 게시할 때
[`OSS 프리뷰 릴리스 체크리스트`](https://github.com/aluminous/cicd-test-harness/blob/main/docs/release-checklist.md)를
사용할 수 있습니다.
