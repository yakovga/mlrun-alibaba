# Copyright 2023 Iguazio
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import http

import pytest
from aioresponses import aioresponses as aioresponses_

import mlrun.common.schemas
import mlrun.config
import mlrun.errors
import mlrun.runtimes.nuclio.api_gateway
import server.api.utils.clients.async_nuclio


@pytest.fixture()
async def api_url() -> str:
    return "http://nuclio-dashboard-url"


@pytest.fixture()
async def nuclio_client(
    api_url,
) -> server.api.utils.clients.async_nuclio.Client:
    auth_info = mlrun.common.schemas.AuthInfo()
    auth_info.username = "admin"
    auth_info.session = "bed854c1-c57751553"
    client = server.api.utils.clients.async_nuclio.Client(auth_info)
    client._nuclio_dashboard_url = api_url
    return client


@pytest.fixture
def mock_aioresponse():
    with aioresponses_() as m:
        yield m


@pytest.mark.asyncio
async def test_nuclio_get_api_gateway(
    api_url,
    nuclio_client,
    mock_aioresponse,
):
    api_gateway = mlrun.runtimes.nuclio.api_gateway.APIGateway(
        functions=["test"], name="test-basic", project="default-project"
    )

    request_url = f"{api_url}/api/api_gateways/test-basic"
    mock_aioresponse.get(
        request_url,
        payload=api_gateway.to_scheme().dict(),
        status=http.HTTPStatus.ACCEPTED,
    )
    r = await nuclio_client.get_api_gateway("test-basic", "default")
    received_api_gateway = mlrun.runtimes.nuclio.api_gateway.APIGateway.from_scheme(r)
    assert received_api_gateway.name == api_gateway.name
    assert received_api_gateway.description == api_gateway.description
    assert received_api_gateway.authentication_mode == api_gateway.authentication_mode
    assert received_api_gateway.functions == api_gateway.functions
    assert received_api_gateway.canary == api_gateway.canary


@pytest.mark.asyncio
async def test_nuclio_store_api_gateway(
    api_url,
    nuclio_client,
    mock_aioresponse,
):
    request_url = f"{api_url}/api/api_gateways/new-gw"
    api_gateway = mlrun.runtimes.nuclio.api_gateway.APIGateway(
        project="default",
        name="new-gw",
        functions=["test-func"],
    )

    mock_aioresponse.put(
        request_url,
        status=http.HTTPStatus.ACCEPTED,
        payload=mlrun.common.schemas.APIGateway(
            metadata=mlrun.common.schemas.APIGatewayMetadata(
                name="new-gw",
            ),
            spec=mlrun.common.schemas.APIGatewaySpec(
                name="new-gw",
                path="/",
                host="test.host",
                upstreams=[
                    mlrun.common.schemas.APIGatewayUpstream(
                        nucliofunction={"name": "test-func"}
                    )
                ],
            ),
        ).dict(),
    )
    await nuclio_client.store_api_gateway(
        project_name="default",
        api_gateway_name="new-gw",
        api_gateway=api_gateway.to_scheme(),
    )
