<?php

declare(strict_types=1);

namespace OCA\CodexQueue\Controller;

use OCA\CodexQueue\BoardStore;
use OCP\AppFramework\Http\Attribute\NoAdminRequired;
use OCP\AppFramework\Http\DataResponse;
use OCP\AppFramework\OCSController;
use OCP\IConfig;
use OCP\IRequest;
use OCP\IUserSession;

final class BridgeController extends OCSController {
    public function __construct(
        IRequest $request,
        private IUserSession $session,
        private BoardStore $board,
        private IConfig $config,
    ) {
        parent::__construct('codex_queue', $request);
    }

    #[NoAdminRequired]
    public function pull(): DataResponse {
        $owner = $this->session->getUser()->getUID();
        return new DataResponse([
            'owner' => $owner,
            'instance' => hash('sha256', $this->config->getSystemValueString('instanceid')),
            'requests' => $this->board->pending($owner, true),
        ]);
    }

    #[NoAdminRequired]
    public function publish(array $snapshot, array $results = []): DataResponse {
        if (count($results) > 20 || strlen(json_encode($snapshot, JSON_THROW_ON_ERROR)) > 8000000) {
            return new DataResponse([], 413);
        }
        foreach ($results as $result) {
            if (!is_array($result) || !is_int($result['id'] ?? null)
                || !is_string($result['outcome'] ?? null) || mb_strlen($result['outcome']) > 2000) {
                return new DataResponse([], 400);
            }
        }
        $snapshot['synced_at'] = gmdate(DATE_ATOM);
        $this->board->publish($this->session->getUser()->getUID(), $snapshot, $results);
        return new DataResponse([]);
    }
}
