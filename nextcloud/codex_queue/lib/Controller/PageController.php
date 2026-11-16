<?php

declare(strict_types=1);

namespace OCA\CodexQueue\Controller;

use OCP\AppFramework\Controller;
use OCP\AppFramework\Http\Attribute\NoAdminRequired;
use OCP\AppFramework\Http\Attribute\NoCSRFRequired;
use OCP\AppFramework\Http\TemplateResponse;
use OCP\IRequest;
use OCP\IURLGenerator;

final class PageController extends Controller {
    public function __construct(IRequest $request, private IURLGenerator $urls) {
        parent::__construct('codex_queue', $request);
    }

    #[NoAdminRequired]
    #[NoCSRFRequired]
    public function index(): TemplateResponse {
        return new TemplateResponse('codex_queue', 'index', [
            'api' => $this->urls->getWebroot() . '/ocs/v2.php/apps/codex_queue/api/v1',
        ]);
    }
}
