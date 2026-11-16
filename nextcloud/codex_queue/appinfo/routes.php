<?php

declare(strict_types=1);

return [
    'routes' => [
        ['name' => 'page#index', 'url' => '/', 'verb' => 'GET'],
    ],
    'ocs' => [
        ['name' => 'board#read', 'url' => '/api/v1/board', 'verb' => 'GET'],
        ['name' => 'board#request', 'url' => '/api/v1/requests', 'verb' => 'POST'],
        ['name' => 'bridge#pull', 'url' => '/api/v1/bridge', 'verb' => 'GET'],
        ['name' => 'bridge#publish', 'url' => '/api/v1/bridge', 'verb' => 'POST'],
    ],
];
