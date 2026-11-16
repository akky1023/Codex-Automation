<?php

declare(strict_types=1);

namespace OCA\CodexQueue\Migration;

use Closure;
use OCP\DB\ISchemaWrapper;
use OCP\DB\Types;
use OCP\Migration\IOutput;
use OCP\Migration\SimpleMigrationStep;

final class Version000300Date20261117000000 extends SimpleMigrationStep {
    public function changeSchema(IOutput $output, Closure $schemaClosure, array $options): ?ISchemaWrapper {
        $schema = $schemaClosure();
        $requests = $schema->getTable('cq_requests');
        $requests->addColumn('session_id', Types::STRING, ['length' => 36, 'notnull' => false]);
        $requests->addColumn('round_no', Types::INTEGER, ['notnull' => false]);
        $requests->addColumn('branch_mode', Types::STRING, ['length' => 16, 'notnull' => false]);
        return $schema;
    }
}
